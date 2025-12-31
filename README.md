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
