#!/usr/bin/env python3
"""Fetch recession-warning signals, score them, and record the result.

Outputs (all under data/):
  latest.json   - today's readings, statuses and overall score (read by the page)
  series.json   - recent history of each raw series, for the charts
  history.csv   - one row per run date; our own record of scores over time

Uses only the Python standard library so the GitHub Action needs no installs.
Data sources need no API key:
  FRED (St. Louis Fed) CSV endpoint, OECD SDMX API.
"""

from __future__ import annotations

import csv
import io
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LATEST = DATA / "latest.json"
SERIES = DATA / "series.json"
HISTORY = DATA / "history.csv"

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"
OECD_CLI = (
    "https://sdmx.oecd.org/public/rest/data/OECD.SDD.STES,DSD_STES@DF_CLI,"
    "/USA.M.LI...AA...H?startPeriod={start}&format=csv"
)

WARNING, WEAK, CALM, UNAVAILABLE = "warning", "weak", "calm", "unavailable"
POINTS = {WARNING: 1.0, WEAK: 0.5, CALM: 0.0, UNAVAILABLE: 0.0}

HISTORY_FIELDS = [
    "run_date", "run_utc", "score", "level", "signals_available",
    "yield_curve_value", "yield_curve_asof", "yield_curve_status",
    "leading_index_value", "leading_index_asof", "leading_index_status",
    "credit_spread_value", "credit_spread_asof", "credit_spread_status",
    "jobless_claims_value", "jobless_claims_asof", "jobless_claims_status",
    "sahm_value", "sahm_asof", "sahm_status",
]


# ---------------------------------------------------------------- fetching

def _get_once(url: str) -> str:
    # FRED's CDN silently drops requests from unrecognised clients (e.g. Python's
    # urllib), so use the real curl binary when it exists (it does on GitHub runners).
    if shutil.which("curl"):
        res = subprocess.run(
            ["curl", "-sS", "-f", "-L", "--max-time", "60", url],
            capture_output=True, text=True,
        )
        if res.returncode != 0:
            raise RuntimeError(res.stderr.strip() or f"curl exit {res.returncode}")
        return res.stdout
    req = urllib.request.Request(url, headers={"User-Agent": "econ-signal-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8")


def http_get(url: str, attempts: int = 3) -> str:
    last_err: Exception | None = None
    for i in range(attempts):
        try:
            return _get_once(url)
        except Exception as err:  # network errors, HTTP 5xx, timeouts
            last_err = err
            time.sleep(5 * (i + 1))
    raise RuntimeError(f"GET failed after {attempts} attempts: {url}: {last_err}")


def fred(series_id: str) -> list[tuple[date, float]]:
    """Return [(date, value)] for a FRED series, skipping missing values."""
    text = http_get(FRED_CSV.format(sid=series_id))
    rows = csv.reader(io.StringIO(text))
    header = next(rows)
    if len(header) < 2:
        raise RuntimeError(f"Unexpected FRED header for {series_id}: {header}")
    out = []
    for r in rows:
        if len(r) < 2 or r[1] in ("", "."):
            continue
        out.append((date.fromisoformat(r[0]), float(r[1])))
    if not out:
        raise RuntimeError(f"No observations for {series_id}")
    out.sort()
    return out


def oecd_cli() -> list[tuple[date, float]]:
    """OECD composite leading indicator for the US (amplitude adjusted, 100 = trend)."""
    start = f"{date.today().year - 12}-01"
    text = http_get(OECD_CLI.format(start=start))
    reader = csv.DictReader(io.StringIO(text))
    out = []
    for r in reader:
        if not r.get("OBS_VALUE"):
            continue
        y, m = r["TIME_PERIOD"].split("-")
        out.append((date(int(y), int(m), 1), float(r["OBS_VALUE"])))
    if not out:
        raise RuntimeError("No OECD CLI observations")
    out.sort()
    return out


# ---------------------------------------------------------------- helpers

def since(obs, days: int, ref: date):
    cutoff = ref - timedelta(days=days)
    return [(d, v) for d, v in obs if d >= cutoff]


def monthly_means(obs):
    buckets: dict[tuple[int, int], list[float]] = {}
    for d, v in obs:
        buckets.setdefault((d.year, d.month), []).append(v)
    return [(date(y, m, 1), sum(vs) / len(vs)) for (y, m), vs in sorted(buckets.items())]


def rolling_mean(obs, n: int):
    out = []
    for i in range(n - 1, len(obs)):
        window = [v for _, v in obs[i - n + 1 : i + 1]]
        out.append((obs[i][0], sum(window) / n))
    return out


def fmt_date(d: date) -> str:
    return d.isoformat()


# ---------------------------------------------------------------- scoring rules
# Each scorer returns a dict with value, asof, status, and a plain-English reason.

def score_yield_curve(obs):
    """10-year minus 3-month Treasury spread (FRED T10Y3M), percentage points.

    warning: 20-day average below 0 (curve inverted)
    weak:    not inverted now, but a monthly average was below 0 within the last
             18 months (recessions have often started soon after the curve
             un-inverts), or the 20-day average is below 0.25 (nearly flat)
    calm:    otherwise
    """
    last_d, last_v = obs[-1]
    avg20 = sum(v for _, v in obs[-20:]) / min(20, len(obs))
    recent_months = [m for m in monthly_means(since(obs, 548, last_d))]
    inverted_months = [m for m, v in recent_months if v < 0]
    if avg20 < 0:
        status = WARNING
        reason = f"Curve is inverted: 20-day average {avg20:+.2f} pts."
    elif inverted_months:
        status = WEAK
        last_inv = max(inverted_months)
        reason = (f"Positive now ({avg20:+.2f} pts) but was inverted as recently as "
                  f"{last_inv.strftime('%b %Y')}. Recessions have often begun after the curve re-steepens.")
    elif avg20 < 0.25:
        status = WEAK
        reason = f"Nearly flat: 20-day average {avg20:+.2f} pts."
    else:
        status = CALM
        reason = f"Normal, upward-sloping curve: 20-day average {avg20:+.2f} pts."
    return {"value": round(last_v, 2), "asof": fmt_date(last_d), "status": status,
            "reason": reason, "metric": round(avg20, 3)}


def score_leading_index(obs):
    """OECD composite leading indicator, US (100 = long-run trend).

    warning: below 100 and lower than 6 months earlier
    weak:    lower than 3 months earlier, or below 100
    calm:    above 100 and not falling
    """
    last_d, last_v = obs[-1]
    v3 = obs[-4][1] if len(obs) >= 4 else last_v
    v6 = obs[-7][1] if len(obs) >= 7 else last_v
    ch3, ch6 = last_v - v3, last_v - v6
    if last_v < 100 and ch6 < 0:
        status = WARNING
        reason = f"Below trend ({last_v:.2f}) and down {ch6:+.2f} over 6 months."
    elif ch3 < 0:
        status = WEAK
        reason = f"Falling: {ch3:+.2f} over 3 months (level {last_v:.2f})."
    elif last_v < 100:
        status = WEAK
        reason = f"Below trend ({last_v:.2f}) but not falling."
    else:
        status = CALM
        reason = f"Above trend ({last_v:.2f}) and rising ({ch3:+.2f} over 3 months)."
    return {"value": round(last_v, 2), "asof": last_d.strftime("%Y-%m"), "status": status,
            "reason": reason, "metric": round(ch6, 3)}


def score_credit_spread(obs):
    """ICE BofA US High Yield option-adjusted spread (FRED BAMLH0A0HYM2), percent.

    warning: 1.5+ pts above its 12-month low, or above 5.0%
    weak:    0.75+ pts above its 12-month low, or above 4.0%
    calm:    otherwise
    """
    last_d, last_v = obs[-1]
    low = min(v for _, v in since(obs, 365, last_d))
    rise = last_v - low
    if rise >= 1.5 or last_v >= 5.0:
        status = WARNING
    elif rise >= 0.75 or last_v >= 4.0:
        status = WEAK
    else:
        status = CALM
    reason = f"{last_v:.2f}%, which is {rise:+.2f} pts above its 12-month low of {low:.2f}%."
    return {"value": round(last_v, 2), "asof": fmt_date(last_d), "status": status,
            "reason": reason, "metric": round(rise, 3)}


def score_jobless_claims(obs):
    """Initial jobless claims (FRED ICSA), weekly, seasonally adjusted.

    Uses the 4-week average versus its lowest point in the past 12 months.
    warning: 20%+ above the 12-month low
    weak:    10%+ above the 12-month low
    calm:    otherwise
    """
    avg4 = rolling_mean(obs, 4)
    last_d, last_v = avg4[-1]
    low = min(v for _, v in since(avg4, 365, last_d))
    rise = last_v / low - 1
    if rise >= 0.20:
        status = WARNING
    elif rise >= 0.10:
        status = WEAK
    else:
        status = CALM
    reason = (f"4-week average {last_v:,.0f}, which is {rise:+.1%} versus its 12-month low "
              f"of {low:,.0f}.")
    return {"value": round(last_v), "asof": fmt_date(last_d), "status": status,
            "reason": reason, "metric": round(rise, 4), "latest_week": round(obs[-1][1])}


def score_sahm(obs):
    """Real-time Sahm rule (FRED SAHMREALTIME), percentage points.

    A confirmation signal, not counted in the forward score.
    warning: 0.50+ (rule triggered; historically a recession is under way)
    weak:    0.30+
    calm:    otherwise
    """
    last_d, last_v = obs[-1]
    if last_v >= 0.5:
        status, reason = WARNING, f"Triggered at {last_v:.2f}. Historically this means a recession has begun."
    elif last_v >= 0.3:
        status, reason = WEAK, f"Rising: {last_v:.2f} (triggers at 0.50)."
    else:
        status, reason = CALM, f"{last_v:.2f}, well below the 0.50 trigger."
    return {"value": round(last_v, 2), "asof": last_d.strftime("%Y-%m"), "status": status,
            "reason": reason, "metric": round(last_v, 3)}


def level_for(score: float) -> str:
    if score >= 3:
        return "high"
    if score >= 2:
        return "elevated"
    return "low"


# ---------------------------------------------------------------- main

SIGNALS = [
    # key, label, fetcher, scorer, counts toward forward score
    ("yield_curve", "Yield curve (10y - 3m)", lambda: fred("T10Y3M"), score_yield_curve, True),
    ("leading_index", "Leading index (OECD CLI)", oecd_cli, score_leading_index, True),
    ("credit_spread", "High-yield credit spread", lambda: fred("BAMLH0A0HYM2"), score_credit_spread, True),
    ("jobless_claims", "Initial jobless claims", lambda: fred("ICSA"), score_jobless_claims, True),
    ("sahm", "Sahm rule (confirmation)", lambda: fred("SAHMREALTIME"), score_sahm, False),
]

CHART_YEARS = 6


def chart_points(key: str, obs):
    """Trim and downsample series for the page so files stay small."""
    cutoff = date.today().replace(year=date.today().year - CHART_YEARS)
    obs = [(d, v) for d, v in obs if d >= cutoff]
    if key in ("yield_curve", "credit_spread"):
        # daily -> weekly (last observation of each ISO week)
        weekly: dict[tuple[int, int], tuple[date, float]] = {}
        for d, v in obs:
            weekly[d.isocalendar()[:2]] = (d, v)
        obs = sorted(weekly.values())
    if key == "jobless_claims":
        obs = rolling_mean(obs, 4)
    return [[d.isoformat(), round(v, 4 if key != "jobless_claims" else 0)] for d, v in obs]


def load_json(path: Path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def main() -> int:
    DATA.mkdir(exist_ok=True)
    previous = load_json(LATEST) or {}
    prev_signals = {s["key"]: s for s in previous.get("signals", [])}
    prev_series = load_json(SERIES) or {}

    signals, series, errors = [], {}, []
    for key, label, fetch, scorer, forward in SIGNALS:
        try:
            obs = fetch()
            result = scorer(obs)
            result["stale"] = False
            series[key] = chart_points(key, obs)
        except Exception as err:
            errors.append(f"{key}: {err}")
            print(f"WARN {key}: {err}", file=sys.stderr)
            if key in prev_signals:
                # keep the last good reading, flagged as stale
                result = {k: v for k, v in prev_signals[key].items()
                          if k not in ("key", "label", "forward", "rule")}
                result["stale"] = True
            else:
                result = {"value": None, "asof": None, "status": UNAVAILABLE,
                          "reason": "Data source unavailable.", "metric": None, "stale": True}
            if key in prev_series:
                series[key] = prev_series[key]
        result.update({"key": key, "label": label, "forward": forward,
                       "rule": " ".join(scorer.__doc__.split())})
        signals.append(result)

    forward = [s for s in signals if s["forward"]]
    score = sum(POINTS[s["status"]] for s in forward)
    available = sum(1 for s in forward if s["status"] != UNAVAILABLE)
    now = datetime.now(timezone.utc)
    run_date = now.date().isoformat()

    latest = {
        "run_date": run_date,
        "run_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "score": score,
        "max_score": len(forward),
        "level": level_for(score),
        "signals_available": available,
        "signals": signals,
        "errors": errors,
    }
    LATEST.write_text(json.dumps(latest, indent=2) + "\n")
    SERIES.write_text(json.dumps(series, separators=(",", ":")) + "\n")

    # history.csv: one row per run date; a re-run on the same day replaces that row
    row = {"run_date": run_date, "run_utc": latest["run_utc"], "score": score,
           "level": latest["level"], "signals_available": available}
    for s in signals:
        row[f"{s['key']}_value"] = "" if s["value"] is None else s["value"]
        row[f"{s['key']}_asof"] = s["asof"] or ""
        row[f"{s['key']}_status"] = s["status"] + (" (stale)" if s["stale"] else "")
    rows = []
    if HISTORY.exists():
        with HISTORY.open(newline="") as f:
            rows = [r for r in csv.DictReader(f) if r.get("run_date") != run_date]
    rows.append(row)
    rows.sort(key=lambda r: r["run_date"])
    with HISTORY.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    print(f"{run_date}: score {score}/{len(forward)} ({latest['level']})")
    for s in signals:
        print(f"  {s['label']:<28} {s['status']:<11} {s['reason']}")
    # Fail the job only if every source failed, so one outage doesn't stop the record.
    return 1 if len(errors) == len(SIGNALS) else 0


if __name__ == "__main__":
    sys.exit(main())
