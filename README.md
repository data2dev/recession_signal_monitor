# Recession signal monitor

A one-page dashboard that scores four leading US recession signals every day, adds the Sahm rule as a confirmation check, and keeps its own CSV record of the scores over time.

**Live page:** https://data2dev.github.io/economic_crash_prediction/

## How it works

- `scripts/update_signals.py` downloads each series, applies the scoring rules below, and writes:
  - `data/latest.json`: today's readings, statuses and overall score (read by the page)
  - `data/series.json`: recent history of each series, for the charts
  - `data/history.csv`: **our own record**, one row per day. A second run on the same day replaces that day's row.
- `.github/workflows/update.yml` runs the script every day at 15:17 UTC and commits the changed data files. It can also be run by hand from the Actions tab ("Run workflow").
- `index.html` is a static page served by GitHub Pages from the `main` branch.

The script uses only the Python standard library, and none of the data sources need an API key. If one source fails, the last good reading is kept and marked "stale", so one outage doesn't break the daily record.

## The signals and scoring rules

Each of the four leading signals scores **1** for a warning, **½** for a weak warning and **0** when calm, for a total out of 4.

| Signal | Source | Warning (1) | Weak warning (½) |
|---|---|---|---|
| Yield curve, 10-year minus 3-month Treasury | FRED `T10Y3M` | 20-day average below 0 (inverted) | Inverted within the last 18 months, or 20-day average below 0.25 |
| Leading index | OECD composite leading indicator, US (amplitude adjusted) | Below 100 and lower than 6 months earlier | Lower than 3 months earlier, or below 100 |
| High-yield credit spread | FRED `BAMLH0A0HYM2` (ICE BofA US High Yield OAS) | 1.5+ pts above its 12-month low, or above 5% | 0.75+ pts above its 12-month low, or above 4% |
| Initial jobless claims, 4-week average | FRED `ICSA` | 20%+ above its 12-month low | 10%+ above its 12-month low |
| Sahm rule (confirmation only, not in the score) | FRED `SAHMREALTIME` | 0.50 or more | 0.30 or more |

Overall level: **low** below 2, **elevated** at 2 to 2.5, **high** at 3 or more.

### Why these thresholds

In a rough review of US recessions since 1969, real recessions usually had three or four of these signals on, at least partly, before or at the start. False alarms (1966–67, 2019, 2022–24) usually had only one or two, almost always the yield curve and the leading index. Credit spreads and jobless claims stayed calm in nearly every false alarm.

A quick check of the claims and yield-curve rules against past data:
- Claims reached "warning" about four months before the 2001 recession, and "weak" a month before the December 2007 start.
- The yield curve was at "warning" or "weak" through both run-ups.
- Claims also gave a false "warning" in mid-2023.

### Notes and limits

- The Conference Board Leading Economic Index is paid data, so this uses the free OECD leading indicator in its place. The two usually move together but can differ.
- FRED keeps only about three years of ICE high-yield spread history, which is enough for the 12-month rule.
- The sample of recessions is small, and several cells in the historical review were judgment calls. The signals have long and variable lead times and do not predict shocks like a pandemic.
- This is an educational tool, not investment advice.

## Running it locally

```bash
python3 scripts/update_signals.py
python3 -m http.server 8000   # then open http://localhost:8000
```
