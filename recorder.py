#!/usr/bin/env python3
"""Groundwater recorder for https://grondwater.webscada.nl/gwmn/

Records hourly groundwater readings for every monitoring well into one CSV
file per well, named after the well's real-world location:

    data/JAF-MON-010 - Rajathurai Rjatheepan - Valalai Atchuvely.csv

Each CSV starts with '#' comment lines carrying the site's metadata (well id,
owner/location, recorded-at) followed by the columns date,time,value -- one
row per hourly reading, site local time, values as served (cmNAP).

Data source (same file the site's "Save CSV" button downloads):
    GET /gwmn/exportcsv.php?loc_code=<WELL>&period=jaar
                       &datumvan=DD-MM-YYYY&datumtot=DD-MM-YYYY&HoogteUnit=cmNAP

Commands:
    update    Append any readings newer than what is already in the CSVs.
    backfill  Load full history from --from (default 01-01-2022) onward.

Legacy flat files (data/<WELL>.csv) are renamed to the new location-based
name automatically on the next run, and leftover empty stray files are
pruned. Python 3.8+ standard library only.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

BASE = "https://grondwater.webscada.nl/gwmn"
PAGE_URL = BASE + "/"
WELL_LIST_URL = BASE + "/a.php?act=locvall"
EXPORT_URL = BASE + "/exportcsv.php"
USER_AGENT = "gwmn-csv-recorder/1.0 (personal data archive)"
DEFAULT_DATA_DIR = "data"
DEFAULT_FROM = "01-01-2022"
MAX_RETRIES = 3
RETRY_BACKOFF = 3  # seconds
META_FILE = "meta.json"
CSV_HEADER = ["date", "time", "value"]
# Units as served by the site; emitted in every CSV's '# units:' header line
# and documented in the README's 'Variables & units' section.
UNITS_GROUNDWATER = "groundwater level in cmNAP (centimetres relative to the NAP sea-level datum, as served by the dashboard)"
UNITS_RAIN = "hourly rainfall in mm (a missing row means no rain in that hour)"
# Station ids look like JAF-MON-010 (groundwater) or MAL-MON-001-RAIN /
# PUT_MON_007-RAIN (rain gauges, mixed dash/underscore forms on the site).
STATION_ID_RE = re.compile(r"[A-Z]+[_-][A-Z]+[_-][0-9]+(?:-RAIN)?$")


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def http_get(url: str) -> bytes:
    """GET with retries and backoff. Returns raw bytes."""
    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except (urllib.error.URLError, OSError) as err:
            last_err = err
            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF * attempt
                print(f"    retry {attempt}/{MAX_RETRIES - 1} after {wait}s: {err}")
                time.sleep(wait)
    raise RuntimeError(f"GET failed after {MAX_RETRIES} attempts: {url}: {last_err}")


# --------------------------------------------------------------------------
# Well discovery: id -> human-readable name
# --------------------------------------------------------------------------

def fetch_well_names() -> dict[str, str]:
    """Well id -> human-readable name (owner/place), from the page's dropdown."""
    html = http_get(PAGE_URL).decode("utf-8", errors="replace")
    names: dict[str, str] = {}
    for value, label in re.findall(
        r'<option value="([A-Z]+[_-][A-Z]+[_-][0-9]+(?:-RAIN)?)">([^<]*)</option>', html
    ):
        # label looks like "JAF-MON-010-Rajathurai Rjatheepan,Valalai Atchuvely"
        # or "JAF-MON-035-RAIN-Near point pedro WSS": strip the id prefix.
        remainder = label[len(value):].lstrip("-").strip() if label.startswith(value) else label.strip()
        name = remainder if remainder and remainder != value else value
        if name:
            names[value] = name
    if not names:
        raise RuntimeError("Could not parse the well dropdown; site format may have changed")
    return names


def fetch_locvall() -> dict:
    """Raw payload of the locvall endpoint (well list + first/min/max/last)."""
    return json.loads(http_get(WELL_LIST_URL).decode("utf-8"))


def fetch_well_ids() -> list[str]:
    """All groundwater well ids from the locvall endpoint (rain gauges excluded)."""
    ids = sorted(
        w
        for w in fetch_locvall().get("locationValues", {})
        if STATION_ID_RE.fullmatch(w) and not w.endswith("-RAIN")
    )
    if not ids:
        raise RuntimeError("No wells found in locvall response; site format may have changed")
    return ids


def fetch_well_first_dates() -> dict[str, date]:
    """Well id -> date of the site's earliest stored reading (locvall 'first')."""
    out: dict[str, date] = {}
    for well, info in fetch_locvall().get("locationValues", {}).items():
        logtime = (info.get("first") or {}).get("logtime")
        if isinstance(logtime, (int, float)) and logtime > 0:
            # epoch seconds; allow +-1 day slack for server timezone
            out[well] = datetime.fromtimestamp(logtime).date()
    return out


def fetch_rain_ids() -> list[str]:
    """All precipitation gauge ids, e.g. ['JAF-MON-035-RAIN', ...]."""
    ids = sorted(w for w in fetch_locvall().get("locationValues", {}) if w.endswith("-RAIN"))
    if not ids:
        raise RuntimeError("No rain gauges found in locvall response; site format may have changed")
    return ids


# --------------------------------------------------------------------------
# Site export parsing
# --------------------------------------------------------------------------

def fetch_readings(well: str, start: date, end: date) -> list[tuple[str, str, str]]:
    """Fetch (date, time, value) rows for one well between start and end.

    Returns rows in the site's served order (chronological), values as
    served (cmNAP), timestamps in the site's local time.
    """
    query = urllib.parse.urlencode(
        {
            "loc_code": well,
            "period": "jaar",
            "datumvan": start.strftime("%d-%m-%Y"),
            "datumtot": end.strftime("%d-%m-%Y"),
            "HoogteUnit": "cmNAP",
        }
    )
    text = http_get(EXPORT_URL + "?" + query).decode("utf-8-sig")
    if "Fatal error" in text or text.lstrip().startswith("<br"):
        # The site's PHP exporter can exhaust its memory limit on long ranges
        # (seen for rain gauges with >= ~15 months). Fail loudly instead of
        # silently parsing a partial response.
        raise RuntimeError("site returned a server error (range too large?)")
    reader = csv.reader(io.StringIO(text), delimiter=";")
    rows: list[tuple[str, str, str]] = []
    for row in reader:
        if len(row) != 3:
            continue  # metadata header lines etc.
        d, t, v = (cell.strip() for cell in row)
        if not d or not t or not v:
            continue
        try:
            datetime.strptime(d, "%d-%m-%Y")
        except ValueError:
            continue
        rows.append((d, t, v))
    return rows


# --------------------------------------------------------------------------
# Human-readable file naming
# --------------------------------------------------------------------------

def display_name(names: dict[str, str], well: str) -> str:
    raw = names.get(well, "")
    cleaned = re.sub(r"\s*,\s*", " - ", raw)
    # Strip dashes/dots/spaces at both ends: Windows silently drops trailing
    # dots and spaces from file names, which can swallow the '.csv' extension.
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -.")
    return cleaned or well


# Characters Windows forbids in file names (locations contain e.g. "A/...").
_ILLEGAL_FS = re.compile(r'[\\/:*?"<>|]+')


def filename_for_well(names: dict[str, str], well: str) -> str:
    safe = _ILLEGAL_FS.sub("-", display_name(names, well))
    return f"{well} - {safe}.csv"


def csv_path(data_dir: str, names: dict[str, str], well: str) -> str:
    return os.path.join(data_dir, filename_for_well(names, well))


# --------------------------------------------------------------------------
# Local CSV storage (one file per well, '#' metadata header inside)
# --------------------------------------------------------------------------

def _meta_store(data_dir: str) -> dict:
    path = os.path.join(data_dir, META_FILE)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_meta_store(data_dir: str, meta: dict) -> None:
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, META_FILE)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, sort_keys=True, ensure_ascii=False)


def _resolve_existing(data_dir: str, names: dict[str, str], well: str) -> str | None:
    """Current file for a well: new name, legacy <WELL>.csv, or <WELL> - *.csv."""
    candidates = [
        csv_path(data_dir, names, well),
        os.path.join(data_dir, f"{well}.csv"),
    ]
    prefix = f"{well} - "
    if os.path.isdir(data_dir):
        for entry in os.listdir(data_dir):
            if entry.startswith(prefix) and entry.endswith(".csv"):
                candidates.append(os.path.join(data_dir, entry))
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def read_stored_rows(data_dir: str, names: dict[str, str], well: str) -> list[tuple[str, str, str]]:
    """Rows from a well's CSV, ignoring '#' comment lines."""
    path = _resolve_existing(data_dir, names, well)
    if not path:
        return []
    rows: list[tuple[str, str, str]] = []
    with open(path, newline="", encoding="utf-8") as fh:
        for record in csv.reader(fh):
            if not record or record[0].lstrip().startswith("#"):
                continue
            if len(record) >= 3 and record[0].strip() == CSV_HEADER[0] and record[1].strip() == CSV_HEADER[1]:
                continue
            if len(record) >= 3:
                rows.append((record[0].strip(), record[1].strip(), record[2].strip()))
    return rows


def _stored_location_line(data_dir: str, names: dict[str, str], well: str) -> str | None:
    """The '# location:' line currently in the well's CSV, if any."""
    path = _resolve_existing(data_dir, names, well)
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("# location:"):
                    return line.strip()
    except OSError:
        pass
    return None


def write_stored_rows(data_dir: str, names: dict[str, str], well: str, rows: list[tuple[str, str, str]]) -> None:
    """Write a well's CSV with a '#' metadata header, at its location-based name."""
    os.makedirs(data_dir, exist_ok=True)
    name = display_name(names, well)
    meta = _meta_store(data_dir)
    meta[well] = {"name": name, "recorded_at": datetime.now().strftime("%d-%m-%Y %H:%M")}
    _save_meta_store(data_dir, meta)
    rows = sorted(rows, key=lambda r: (datetime.strptime(r[0], "%d-%m-%Y"), r[1]))
    with open(csv_path(data_dir, names, well), "w", newline="", encoding="utf-8") as fh:
        # '#' comment lines are written raw: csv.writer would quote any line
        # containing a comma, but these lines must stay unquoted to match the
        # headers already in the archive and read cleanly as comments.
        units = UNITS_RAIN if well.endswith("-RAIN") else UNITS_GROUNDWATER
        fh.write(f"# well_id: {well}\r\n")
        fh.write(f"# location: {name}\r\n")
        fh.write(f"# recorded_at: {meta[well]['recorded_at']} (site local time)\r\n")
        fh.write(f"# units: {units}\r\n")
        writer = csv.writer(fh)
        writer.writerow(CSV_HEADER)
        writer.writerows(rows)


def migrate_legacy_files(data_dir: str, names: dict[str, str]) -> None:
    """Rename flat <well>.csv files to '<well> - <Location>.csv' (once)."""
    if not os.path.isdir(data_dir):
        return
    for well in sorted(names):
        target = csv_path(data_dir, names, well)
        if os.path.exists(target):
            continue
        stale: str | None = None
        legacy = os.path.join(data_dir, f"{well}.csv")
        prefix = f"{well} - "
        if os.path.exists(legacy):
            stale = legacy
        elif os.path.isdir(data_dir):
            for entry in os.listdir(data_dir):
                if entry.startswith(prefix) and entry.endswith(".csv"):
                    stale = os.path.join(data_dir, entry)
                    break
        if stale:
            os.rename(stale, target)
            print(f"  renamed {os.path.basename(stale)} -> {os.path.basename(target)}")


def prune_stray_files(data_dir: str, names: dict[str, str]) -> None:
    """Delete leftover 0-byte '<WELL>...' files lacking the .csv extension.

    Earlier versions could leave such strays behind (e.g. a display name whose
    trailing dot made Windows swallow the extension). Empty ones are safe to
    remove; anything non-empty is kept and flagged for manual inspection.
    """
    if not os.path.isdir(data_dir):
        return
    well_re = re.compile(r"([A-Z]+[_-][A-Z]+[_-][0-9]+(?:-RAIN)?)")
    for entry in os.listdir(data_dir):
        path = os.path.join(data_dir, entry)
        if not os.path.isfile(path) or entry.endswith(".csv") or entry == META_FILE:
            continue
        match = well_re.search(entry)
        if not match or match.group(1) not in names:
            continue
        size = os.path.getsize(path)
        if size == 0:
            os.remove(path)
            print(f"  removed stray empty file {entry}")
        else:
            print(f"  WARNING: non-empty stray file kept for review: {entry} ({size} bytes)")


# --------------------------------------------------------------------------
# Core record logic
# --------------------------------------------------------------------------

def record_well(
    well: str,
    start: date,
    end: date,
    data_dir: str,
    names: dict[str, str],
    chunk_days: int = 366,
) -> int:
    """Ensure one well's CSV covers start..end. Returns rows added.

    Fetches in sub-chunks of chunk_days (shorter for rain gauges, whose
    server-side export crashes on long ranges).
    """
    stored = read_stored_rows(data_dir, names, well)
    stored_keys = {(d, t) for d, t, _ in stored}

    fetched: list[tuple[str, str, str]] = []
    chunk_start = start
    try:
        while chunk_start <= end:
            chunk_end = min(chunk_start + timedelta(days=chunk_days - 1), end)
            fetched.extend(fetch_readings(well, chunk_start, chunk_end))
            chunk_start = chunk_end + timedelta(days=1)
            time.sleep(0.1)
    except Exception:
        if fetched:
            # Keep whatever completed before the failure, then propagate.
            new_partial = [r for r in fetched if (r[0], r[1]) not in stored_keys]
            if new_partial:
                write_stored_rows(data_dir, names, well, stored + new_partial)
                print(f"  {well}: +{len(new_partial)} rows before failure (partial)")
        raise

    if not fetched:
        print(f"  {well}: no data returned for {start:%d-%m-%Y}..{end:%d-%m-%Y}")
        return 0

    new_rows = [r for r in fetched if (r[0], r[1]) not in stored_keys]
    want_location = f"# location: {display_name(names, well)}"
    if stored and not new_rows and _stored_location_line(data_dir, names, well) == want_location:
        print(f"  {well}: up to date ({len(stored)} rows)")
        return 0

    merged = stored + new_rows
    write_stored_rows(data_dir, names, well, merged)
    note = "" if new_rows else " (header refreshed)"
    print(f"  {well}: +{len(new_rows)} rows{note} -> {os.path.basename(csv_path(data_dir, names, well))} ({len(merged)} total)")
    return len(new_rows)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def parse_ddmmyyyy(value: str) -> date:
    return datetime.strptime(value, "%d-%m-%Y").date()


def resolve_wells(args: argparse.Namespace) -> list[str]:
    if args.wells:
        return [w.strip() for w in args.wells.split(",") if w.strip()]
    if os.path.isfile(args.wells_file):
        with open(args.wells_file, encoding="utf-8") as fh:
            wells = [line.strip() for line in fh if line.strip() and not line.lstrip().startswith("#")]
        if wells:
            return wells
    print("Discovering stations from site ...")
    return fetch_rain_ids() if getattr(args, "rain", False) else fetch_well_ids()


def cmd_update(args: argparse.Namespace) -> int:
    """Incremental: fetch only from the last stored row's day onward."""
    today = date.today()
    wells = resolve_wells(args)
    names = fetch_well_names()
    migrate_legacy_files(args.data_dir, names)
    prune_stray_files(args.data_dir, names)
    print(f"{len(wells)} wells; incremental update to {today:%d-%m-%Y}")
    failures = 0
    total_new = 0
    for i, well in enumerate(wells, 1):
        stored = read_stored_rows(args.data_dir, names, well)
        if stored:
            # Re-fetch from the last stored day: up to 24 rows/day means a
            # partial last day must be completed.
            start = parse_ddmmyyyy(stored[-1][0])
        else:
            # Unknown start: default origin keeps update usable on an empty dir.
            start = parse_ddmmyyyy(args.from_date)
        print(f"[{i}/{len(wells)}] {well} ({display_name(names, well)})")
        try:
            total_new += record_well(
                well,
                start,
                today,
                args.data_dir,
                names,
                chunk_days=90 if well.endswith("-RAIN") else 366,
            )
        except Exception as err:
            failures += 1
            print(f"  {well}: ERROR {err}")
        time.sleep(args.delay)
    print(f"Done: {total_new} new rows, {failures} failed well(s)")
    return 1 if failures else 0


def cmd_backfill(args: argparse.Namespace) -> int:
    """Initial history load in yearly chunks."""
    start = parse_ddmmyyyy(args.from_date)
    end = date.today()
    wells = resolve_wells(args)
    names = fetch_well_names()
    migrate_legacy_files(args.data_dir, names)
    prune_stray_files(args.data_dir, names)
    print(f"Backfilling {len(wells)} well(s) from {start:%d-%m-%Y} to {end:%d-%m-%Y}")
    failures = 0
    total_new = 0
    site_first = fetch_well_first_dates()
    for i, well in enumerate(wells, 1):
        print(f"[{i}/{len(wells)}] {well} ({display_name(names, well)})")
        stored = read_stored_rows(args.data_dir, names, well)
        location_fresh = _stored_location_line(args.data_dir, names, well) == f"# location: {display_name(names, well)}"
        if stored and location_fresh:
            first, last = parse_ddmmyyyy(stored[0][0]), parse_ddmmyyyy(stored[-1][0])
            # Covered if we already hold everything from the requested start
            # (or everything the site has, when its history begins later).
            earliest = site_first.get(well)
            head_ok = first <= start or (earliest is not None and first <= earliest + timedelta(days=2))
            tail_ok = last >= end - timedelta(days=1)
            if head_ok and tail_ok:
                print(f"  {well}: CSV already covers {start:%d-%m-%Y}..{end:%d-%m-%Y}; skipping")
                continue
        chunk_start = start
        try:
            while chunk_start <= end:
                chunk_end = min(date(chunk_start.year, 12, 31), end)
                total_new += record_well(
                    well,
                    chunk_start,
                    chunk_end,
                    args.data_dir,
                    names,
                    chunk_days=90 if well.endswith("-RAIN") else 366,
                )
                chunk_start = chunk_end + timedelta(days=1)
                time.sleep(args.delay)
        except Exception as err:
            failures += 1
            print(f"  {well}: ERROR {err}")
        time.sleep(args.delay)
    print(f"Done: {total_new} new rows, {failures} failed well(s)")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help=f"CSV output dir (default: {DEFAULT_DATA_DIR})")
        p.add_argument("--wells", help="comma-separated well ids, e.g. JAF-MON-010,JAF-MON-014")
        p.add_argument("--wells-file", default="wells.txt", help="optional file with one well id per line")
        p.add_argument("--delay", type=float, default=0.5, help="seconds between requests (default: 0.5)")
        p.add_argument("--rain", action="store_true", help="record precipitation gauges (*-RAIN) instead of groundwater wells; defaults to data-rain/")

    p_update = sub.add_parser("update", help="incremental update of all CSVs")
    add_common(p_update)
    p_update.add_argument("--from-date", default=DEFAULT_FROM, help="fallback start for wells with no CSV yet")
    p_update.set_defaults(func=cmd_update)

    p_backfill = sub.add_parser("backfill", help="load full history from a start date")
    add_common(p_backfill)
    p_backfill.add_argument("--from", dest="from_date", default=DEFAULT_FROM, help=f"start date DD-MM-YYYY (default: {DEFAULT_FROM})")
    p_backfill.set_defaults(func=cmd_backfill)

    args = parser.parse_args(argv)
    if args.rain and args.data_dir == DEFAULT_DATA_DIR:
        args.data_dir = "data-rain"
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
