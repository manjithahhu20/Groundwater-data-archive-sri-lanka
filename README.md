# Groundwater CSV Recorder

Records hourly groundwater readings from the Sri Lanka National Groundwater
Monitoring Network dashboard (<https://grondwater.webscada.nl/gwmn/>) into one
CSV file per monitoring well.

```text
data/JAF-MON-010 - Rajathurai Rjatheepan - Valalai Atchuvely.csv
data/JAF-MON-014 - S.Thayanatharaja - Kamparmalai.csv
...
```

Each CSV starts with `#` comment lines (ignored by pandas/Excel parsers that
respect comments, harmless otherwise), then one row per hourly reading:

```csv
# well_id: JAF-MON-010
# location: Rajathurai Rjatheepan - Valalai Atchuvely
# recorded_at: 17-09-2026 10:56 (site local time)
date,time,value
01-09-2026,00:30,16.69
...
```

- The well ID stays at the front of the filename, so files remain unique,
  sortable, and stable even if the site renames a location.
- `data/meta.json` maps well ID -> display name, in case you need to resolve
  IDs to filenames programmatically.
- Files created before this naming scheme (`<WELL>.csv`) are renamed
  automatically on the next `update`/`backfill` run.

## Usage

Python 3.8+ — no third-party packages needed.

```bash
# Initial full history for every well (first run)
python recorder.py backfill

# Later: append only what's new (idempotent, safe to re-run)
python recorder.py update

# Just some wells
python recorder.py update --wells JAF-MON-010,JAF-MON-014

# Precipitation gauges (13 *-RAIN stations, recorded into data-rain/)
python recorder.py backfill --rain
python recorder.py update --rain
```

Options common to both commands:

| Option | Default | Description |
|---|---|---|
| `--data-dir` | `data` | Output directory for the per-well CSVs |
| `--wells` | all wells | Comma-separated well ids |
| `--wells-file` | `wells.txt` | Optional file with one well id per line (used if present) |
| `--delay` | `0.5` | Pause between HTTP requests, seconds |

The well list is discovered automatically from the site each run, so new wells
are picked up without code changes. The site lists 113 stations: the 100
groundwater wells recorded here plus 13 rain gauges (`*-RAIN`), which are
intentionally excluded. Run `backfill` once for a new well's
history; `update` only fills from its last stored row onward. A `backfill` for
wells whose CSVs already cover the requested range is skipped instantly.

Values are "as served" by the site (cmNAP reference level); timestamps are the
site's local time. Full history goes back to mid-2022 (~14,000 rows/well/year).

Rain-gauge CSVs use the same format with rainfall in mm; rows exist only for
hours with rain. Four of the 13 gauges are unnamed on the site, so their files
are named after the gauge ID. The site's exporter crashes (PHP memory limit)
on long date ranges for rain gauges, so those fetches are chunked to 90 days
automatically. Note: gauge JAF-MON-035-RAIN's export ends on 06-08-2026 even
though the dashboard advertises newer telemetry — nothing more is served by
the site's CSV export for it.

## Scheduling

Any daily trigger works, e.g. cron at 03:00:

```cron
0 3 * * * cd /path/to/repo && python recorder.py update >> update.log 2>&1
```

## GitHub Actions

The repo includes `.github/workflows/update.yml`. Once this project is pushed
to GitHub:

1. Open the **Actions** tab and run **"Update groundwater data"** manually
   (workflow_dispatch) to confirm it works.
2. When ready for automatic daily runs, un-comment the `schedule:` block at
   the top of the workflow. Times are UTC — adjust to catch the day's last
   readings (Sri Lanka is UTC+5:30).
3. Each run appends new rows to the per-well CSVs and commits them back to the
   repository with `[skip ci]` so it doesn't re-trigger itself.

To record without committing to a repo, run the workflow with the
`commit_csvs` input unchecked — it will just exercise the fetch and print a
summary.

## Data source

Reverse-engineered endpoints (the same ones the dashboard's JavaScript uses):

- Well list + last/min/max values: `GET /gwmn/a.php?act=locvall`
- Export: `GET /gwmn/exportcsv.php?loc_code=<WELL>&period=jaar&datumvan=DD-MM-YYYY&datumtot=DD-MM-YYYY&HoogteUnit=cmNAP`
  — UTF-8 (BOM), semicolon-delimited, 8 metadata lines then `date;time;value`.

Values are recorded exactly as served; no unit conversion or timezone
conversion is applied.
