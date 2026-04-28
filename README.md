# R2C-Tracker

A FastAPI-based flight log aggregator for SAR operations.

## Setup
1. `pip install -r requirements.txt`
2. `uvicorn main:app --reload --port $TRACKER_PORT`

## Environment Variables
- `TRACKER_PORT`: unrestricted port number (i.e. 8080)
- `TRACKER_API_KEY`: Key required for /upload
- `TRACKER_ADMIN_PASS`: Password for the /admin portal
- `DB_URL`: URL for the postgress database - omit to use local filesystem instead.
- define everything in .env file and pull into shell via .env prior to start.

## Features
- GeoJSON upload parsing w/overlap detection for data integrity.
- Weather stats during flight via archive-open-meteo
- Day/Night calculation via Suncalc
- Automated Leaderboard & recent flights.
- Basic Admin and CSV Export
- Optional RID2Caltopo multi-zone coordination hub on `/ws/r2c`

## Coordination Docs
- [R2C protocol and robustness guide](/Users/kjt/Projects/r2c-tracker/R2C_PROTOCOL.md)
- [Google Cloud reproduction/setup guide](/Users/kjt/Projects/r2c-tracker/GCLOUD_SETUP.md)

## Flight Archive Recovery
When rebuilding the tracker from archived flight logs:

1. Download the full admin CSV export from `/export` as a backup and metadata source.
2. Download the flight log archive from `/flightlogs/archive`.
3. Empty the `flights` table and clear the `flightlogs-vol` bucket contents.
4. Deploy the current app version so the `archive_relpath` schema and archive import tools are available.
5. In `/admin`, run `Import from Flight Log Archive` using the saved `.tgz/.tar.gz/.tar` file.
6. In `/admin`, run `Backfill Weather and Metadata from CSV` using the saved full admin CSV.

Notes:
- Archive import rebuilds the database from GeoJSON flight logs and writes fresh archive files named with the new DB flight ID.
- CSV backfill restores weather and other DB-only fields when a rebuilt flight can be matched safely.
- Some historically deleted or mislabeled flights may remain unmatched during CSV backfill, which is expected.

## Tests
Run the coordination tests with:

`python3 -m unittest discover -s tests -p "test_*.py"`

These exercise owner selection, relayed sightings, and heartbeat/lease expiry
without requiring filesystem persistence.

The suite also covers deterministic tie-breaking, map isolation, lease refresh
behavior, and owner-release edge cases for the multi-zone coordination path.

For higher-confidence release checks, `tests/test_r2c_scenarios.py` simulates
multi-zone timelines with overlapping drone sightings, disconnect/expiry
handoffs, and deterministic ownership assertions.
