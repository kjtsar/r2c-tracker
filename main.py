import os
import io
import re
import sys
import math
import csv
import json
import requests
import asyncio
import warnings
import traceback
import logging
import numpy as np
from pprint import pprint
from datetime import datetime, date, timedelta, timezone
from typing import Optional

from fastapi import Security, Depends, FastAPI, Request, HTTPException, Query, Form, status, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.security.api_key import APIKeyHeader
from starlette.status import HTTP_403_FORBIDDEN
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, Column, Integer, String, Float, Boolean, DateTime, desc, func, or_, and_
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from suncalc import get_times

# --- CONFIGURATION & DATABASE SETUP ---
API_KEY_NAME = "X-SAR-Token"
API_SECRET_TOKEN = os.environ.get("TRACKER_API_KEY", "super-secret-shared-token")
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)
ADMIN_USER = os.environ.get("TRACKER_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("TRACKER_ADMIN_PASS", "F1yM0re")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DB_URL = os.environ.get("DATABASE_URL", "sqlite:///./test.db") # Defaults to local file if no Cloud SQL
engine = create_engine(DB_URL)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

class Flight(Base):
    __tablename__ = "flights"
    id = Column(Integer, primary_key=True)
    sar_id = Column(String, default="undefined")
    uas = Column(String, default="")
    incident = Column(String, default="")
    op_period = Column(String, default="")
    map_id = Column(String, default="")
    start_time = Column(DateTime)
    end_time = Column(DateTime)
    hours = Column(Float, default=0.0)
    distance_mi = Column(Float, default=0.0)
    temp_f = Column(Float, default=0.0)
    rhum_pct = Column(Float, default=0.0)
    dewpt_f = Column(Float, default=0.0)
    precip_in = Column(Float, default=0.0)
    wind_mph = Column(Float, default=0.0)
    gusts_mph = Column(Float, default=0.0)
    cloudcvr_pct = Column(Float, default=0.0)
    night_flag = Column(Boolean, default=False)
    
Base.metadata.create_all(bind=engine)

app = FastAPI()

# --- CORS MIDDLEWARE ---
# This allows your drone app to send PUT requests without being blocked
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory="templates")

# --- HELPER FUNCTIONS ---
security = HTTPBasic()
def check_admin(credentials: HTTPBasicCredentials = Depends(security)):
    if credentials.username != ADMIN_USER or credentials.password != ADMIN_PASSWORD:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return credentials.username

def parse_prop(prop):
    pattern = r"(1?[sS][aA][rR][0-9]+)([^_]*)_?.*"
    incident = prop["r2c_prop"]["incident"]
    op_period = prop["r2c_prop"]["op_period"]
    map_id = prop["r2c_prop"]["map_id"]
    mid = prop["r2c_prop"]["mid"]
    match = re.match(pattern, mid)
    if match:
        sar_id = match.group(1)
        uas = match.group(2)
    if not uas:
        uas = prop["r2c_prop"]["model"]
    if not sar_id or not uas:
        title = prop["title"]  # legacy tracks should at least have a title/track-label
        match = re.match(pattern, title)
        if match:
            sar_id = match.group(1)
            uas = match.group(2)
    if not sar_id:
        sar_id = "unknown"
    return {"incident" : incident, "op_period" : op_period, "sar_id" : sar_id, "uas" : uas, "map_id" : map_id}

def get_weather(lat, lon, dt):
    try:
        tz = datetime.now(timezone.utc).astimezone().tzinfo.tzname(None)
        d_str = dt.strftime("%Y-%m-%d")
        dt_str = dt.strftime("%Y-%m-%dT%H:%M")
        url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&timezone={tz}&start_date={d_str}&end_date={d_str}&start_hour={dt_str}&end_hour={dt_str}&hourly=temperature_2m,relative_humidity_2m,dew_point_2m,precipitation,wind_speed_10m,wind_gusts_10m,cloud_cover&temperature_unit=fahrenheit&wind_speed_unit=mph&precipitation_unit=inch"
        res = requests.get(url).json()
        temp = res['hourly']['temperature_2m'][0]
        hum = res['hourly']['relative_humidity_2m'][0]
        dew = res['hourly']['dew_point_2m'][0]
        precip = res['hourly']['precipitation'][0]
        wind = res['hourly']['wind_speed_10m'][0]
        gusts = res['hourly']['wind_gusts_10m'][0]
        cloud = res['hourly']['cloud_cover'][0]
    except:
        temp = ""; hum = ""; precip = ""; dew = ""; wind = ""; gusts = ""; cloud = ""

    return {"temp" : temp, "hum" : hum, "precip" : precip, "dew" : dew, "wind" : wind, "gusts" : gusts, "cloud" : cloud}

def calculate_distance(coords):
    # coords is a list of [lon, lat, alt, ts]
    total_dist_km = 0.0
    
    for i in range(len(coords) - 1):
        # Haversine Formula
        lon1, lat1 = float(coords[i][0]), float(coords[i][1])
        lon2, lat2 = float(coords[i+1][0]), float(coords[i+1][1])

        radius = 6371 # Earth radius in km
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        if (dlat != 0 and dlon != 0):
            a = (math.sin(dlat / 2) * math.sin(dlat / 2) +
                 math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
                 math.sin(dlon / 2) * math.sin(dlon / 2))
            c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
            total_dist_km += radius * c
            
        
    # Convert km to miles w/~1/2' resolution:
    return round(total_dist_km * 0.621371, 4)

async def get_api_key(header_value: str = Depends(api_key_header)):
    if header_value == API_SECRET_TOKEN:
        return header_value
    else:
        raise HTTPException(
            status_code=HTTP_403_FORBIDDEN, detail="Could not validate credentials"
            )

# --- ROUTES ---
@app.put("/upload")
async def upload(request: Request):
    api_key: str = Depends(get_api_key) # activate the token check.
    raw_body = await request.body()
    try:
        data = await request.json()
        if not data:
            raise ValueError("No data received in payload")
        sar_id = data.get("sar_id", "unknown")
        
    except ValueError as ve:
        logger.warning(f"Validation Error: {ve}")
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        error_details = traceback.format_exc()
        logger.error(f"Exception in /upload:\n{error_details}")
        raise HTTPException(status_code=500, detail=f"Server Error: {str(e)}")
        
    # Extract start time, and duration as well as coordinates from GeoJSON:
    timestamps = []
    coords_ref = None
    for f in data.get("features", []):
        if coords_ref: raise HTTPException(400, "Only one track supported per log file.")
        prop = f["properties"]
        c_list = f["geometry"]["coordinates"]
        for c in c_list:
            if len(c) >= 4:
                timestamps.append(int(c[3]))
                if not coords_ref: coords_ref = (float(c[1]), float(c[0])) # lat, lon
    if not timestamps: raise HTTPException(400, "No timestamps found")
    if not prop: raise HTTPException(400, "No properties found")
    start_ts = min(timestamps)
    end_ts = max(timestamps)
    start_time = datetime.fromtimestamp(start_ts / 1000.0)
    end_time = datetime.fromtimestamp(start_ts / 1000.0)
    duration_hrs = round((end_ts - start_ts) / 3600000.0, 3) # ~3.6 seconds resolution
    spec = parse_prop(prop)

    # Make sure flight doesn't overlap an existing flight (prevent duplicate submissions):
    allow_duplicates = True
    db = SessionLocal()
#    print(f"XYZZY: Checking overlap for {spec['sar_id']} between {start_time} and {end_time}", file=sys.stderr)
    if not allow_duplicates:
        existing_overlap = db.query(Flight).filter(
            Flight.sar_id == spec['sar_id']
        ).filter(
            or_(
                # 1. New start falls inside an existing flight
                and_(Flight.start_time <= start_time, Flight.end_time > start_time),
                # 2. New end falls inside an existing flight
                and_(Flight.start_time < end_time, Flight.end_time >= end_time),
                # 3. New flight completely swallows an existing flight
                and_(Flight.start_time >= start_time, Flight.end_time <= end_time)
            ) 
        ).first()
        if existing_overlap:
            raise HTTPException(
                status_code=409,
                detail=f"Conflict: This log overlaps with an existing entry for {spec['sar_id']}"
            )

    # Determine Day/Night
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=RuntimeWarning)
        # suppress warnings from suncalc:
        sun_times = get_times(start_time, coords_ref[0], coords_ref[1])
    sunset = sun_times.get('sunset')
    sunrise = sun_times.get('sunrise')
    if sunset and not isinstance(sunset, float):
        night_flag = (start_time < sun_times['sunrise'] or start_time > sun_times['sunset'])
    else:
        print(f"Warning: Solar calculation failed for coordinates {coords_ref[0]}, {coords_ref[1]}", file=sys.stderr)
        night_flag = False # default to day if suncalc
        
    distance = calculate_distance(c_list)
    w = get_weather(coords_ref[0], coords_ref[1], start_time)

    # 4. Save to DB
    new_flight = Flight(sar_id=spec["sar_id"], start_time=start_time, end_time=end_time, hours=duration_hrs,
                        incident=spec["incident"], op_period = spec["op_period"],
                        uas = spec["uas"], map_id = spec["uas"], temp_f = w["temp"],
                        rhum_pct = w["hum"], dewpt_f = w["dew"], precip_in = w["precip"],
                        wind_mph = w["wind"], gusts_mph = w["gusts"],
                        cloudcvr_pct = w["cloud"], night_flag = night_flag,
                        distance_mi = distance)
    db.add(new_flight)
    db.commit()
    db.close()
    
    return {"status": "Logged",
            "hours": duration_hrs,
            "night": night_flag,
            "distance_mi": distance,
            "spec": spec,
            "weather": w
            }

@app.get("/", response_class=HTMLResponse)
async def public_dashboard(
        request: Request,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None
):
    db = SessionLocal()
    query = db.query(Flight)

    # Apply date filters if they exist
    if start_date:
        query = query.filter(Flight.start_time >= datetime.combine(start_date, datetime.min.time()))
    if end_date:
        query = query.filter(Flight.start_time <= datetime.combine(end_date, datetime.max.time()))
    # Group by pilot, sum hours
    leaderboard = query.with_entities(
        Flight.sar_id,
        func.sum(Flight.hours).label("total_hours"),
        func.sum(Flight.distance_mi).label("total_miles"),
        func.max(Flight.start_time).label("last_active")).group_by(Flight.sar_id).order_by(desc("total_hours")).all()
    #flights = query.order_by(Flight.start_time).limit(50).all()
    flights = query.order_by(Flight.start_time).all()
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "flights": flights,
        "leaderboard": leaderboard,
        "start_date" : start_date,
        "end_date" : end_date
    })

# List the admin page
@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request, user: str = Depends(check_admin)):
    db = SessionLocal()
    flights = db.query(Flight).order_by(Flight.start_time.desc()).all()
    return templates.TemplateResponse("admin.html", {"request": request, "flights": flights})

@app.post("/admin/edit/{flight_id}")
async def edit_flight(
        flight_id: int,
        new_sar_id: str = Form(...),
        user: str = Depends(check_admin)):
    db = SessionLocal()
    flight = db.query(Flight).filter(Flight.id == flight_id).first()
    if flight:
        flight.sar_id = new_sar_id.upper().strip()
        db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/delete/{flight_id}")  # Must be .post
async def delete_flight(flight_id: int, user: str = Depends(check_admin)):
    db = SessionLocal()
    flight = db.query(Flight).filter(Flight.id == flight_id).first()
    if flight:
        db.delete(flight)
        db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)
    
@app.get("/export", response_class=Response, responses={
    200: {
        "content": {"text/csv": {}},
        "description": "Return a CSV file of all flight logs.",
    }
})
async def export(
        start_date: Optional[date] = None,
        end_date: Optional[date] = None
):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    db = SessionLocal()
    query = db.query(Flight)
    if start_date:
        query = query.filter(Flight.start_time >= datetime.combine(start_date, datetime.min.time()))
    if end_date:
        query = query.filter(Flight.start_time <= datetime.combine(end_date, datetime.max.time()))

    
    flights = query.order_by(Flight.start_time.desc()).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Flight", "Sar Id", "Date", "UAS", "Incident", "Op Period", "Map Id", "Hours", "Distance (mi)",
                     "Temp (F)", "Humidity (%)", "Dew Pt (F)", "Precip (in)", "Wind (mph)", "Gusts (mph)",
                     "Cloud Cover (%)", "Night"])
    
    for f in flights:
        writer.writerow([f.id, f.sar_id, f.start_time, f.uas, f.incident, f.op_period, f.map_id, f.hours, f.distance_mi,
                         f.temp_f, f.rhum_pct, f.dewpt_f, f.precip_in, f.wind_mph, f.gusts_mph,
                         f.cloudcvr_pct, f.night_flag])
    
    csv_content = output.getvalue()
    return Response(
        content=csv_content, 
        media_type="text/csv", 
        headers={"Content-Disposition": f"attachment; filename=r2c_audit{timestamp}.csv"}
    )
