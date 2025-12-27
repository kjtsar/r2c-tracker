# R2C-Tracker

A FastAPI-based flight log aggregator for SAR operations.

## Setup
1. `pip install -r requirements.txt`
2. `uvicorn main:app --reload`

## Environment Variables
- `SAR_API_KEY`: Key required for /upload
- `ADMIN_PASSWORD`: Password for the /admin portal

## Features
- GeoJSON upload parsing
- Day/Night calculation via Suncalc
- Automated Leaderboard & CSV Export
- Overlap detection for data integrity
