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
from zoneinfo import ZoneInfo
from typing import Optional, Annotated

from fastapi import Security, Depends, FastAPI, Request, HTTPException, Query, Form
from fastapi import status, Response, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.security.api_key import APIKeyHeader
from starlette.status import HTTP_403_FORBIDDEN
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, MetaData, Table
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, desc, func, or_, and_
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from suncalc import get_times
from google.cloud.sql.connector import Connector, IPTypes

# --- CONFIGURATION & DATABASE SETUP ---

DB_URL = os.environ.get("DATABASE_URL", "sqlite:///./test.db") # Defaults to local file if no Cloud SQL
DB_USER = os.environ.get("DB_USER", "undefined")
DB_PASS = os.environ.get("DB_PASS", "undefined")
DB_NAME = os.environ.get("DB_NAME", "undefined")
API_KEY_NAME = "X-SAR-Token"
TRACKER_API_KEY = os.environ.get("TRACKER_API_KEY", "replace-with-token")
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)
TRACKER_ADMIN_USER = os.environ.get("TRACKER_ADMIN_USER", "admin")
TRACKER_ADMIN_PASS = os.environ.get("TRACKER_ADMIN_PASS", "replace-with-password")
ALLOW_DUPLICATES = os.environ.get("TRACKER_ALLOW_DUPLICATES", False)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def getconn():
    connector = Connector()
    return connector.connect(DB_URL, "pg8000", user=DB_USER, password=DB_PASS, db=DB_NAME, ip_type=IPTypes.PUBLIC)

engine = create_engine("postgresql+pg8000://", creator=getconn)

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
    timeofday = Column(String, default="day")
    
Base.metadata.create_all(bind=engine)

class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        for connection in self.active_connections:
            await connection.send_text(message)
        
manager = ConnectionManager()
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
    if credentials.username != TRACKER_ADMIN_USER or credentials.password != TRACKER_ADMIN_PASS:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return credentials.username

def get_time_of_day(utc_time, lat, lng):
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=RuntimeWarning)
        # suppress warnings from suncalc:
        sun_times = get_times(utc_time, lng, lat)
    # Sort keys by time to determine the active range
    # We filter for common phases returned by suncalc-py
    phases = [
        (sun_times['night_end'], "Pre-dawn"),
        (sun_times['nautical_dawn'], "Nautical Dawn"),
        (sun_times['dawn'], "Civil Dawn"),
        (sun_times['sunrise'], "Sunrise"),
        (sun_times['sunrise_end'], "Early Morning"),
        (sun_times['golden_hour_end'], "Morning"),
        (sun_times['solar_noon'], "Afternoon"),
        (sun_times['golden_hour'], "Late Afternoon"),
        (sun_times['sunset_start'], "Sunset"),
        (sun_times['sunset'], "Civil Dusk"),
        (sun_times['dusk'], "Nautical Dusk"),
        (sun_times['nautical_dusk'], "Astronomical Dusk"),
        (sun_times['night'], "Night")
    ]
    
    # Sort by the datetime value
    phases.sort(key=lambda x: x[0])

    # Find the current phase
    timeofday_str = "Night"  # Default for times before the first phase (early AM)
    for phase_time, description in phases:
        if utc_time >= phase_time:
            timeofday_str = description
        else:
            break

    return timeofday_str

def parse_prop(prop):
    """Parse a LineString 'properties' dictionary.
    Args:
        prop (dictionary):      Expects a legacy form Caltopo geo-json 
                                properties dict. If generated by RID2Caltopo
                                1.0.5 or later, will include additional drone-
                                specific parameters in a 'r2c_prop' child dict.
    Returns:
        Dictionary containing:
        'incident'  (string):   Optional incident identifier or "Training".
        'op_period' (string):   Optionally a numbered operational period.
        'sar_id' (string):      Callsign of the form '1SAR7'
        'uas' (string):         Shorthand UAS description - usually follows the
                                sar_id.
        'mid' (string):         Mapped ID - this is the normal prefix used in 
                                the track label.
        'rid' (string):         Remote ID - Ground Truth unique identifier per
                                drone.
        'map_id' (string):      ID of the caltopo map - if specified.
        'distance_mi' (float):  precalculated distance - if available.
    """
    pattern = r"(1?[sS][aA][rR][0-9]+)([^_]*)_?.*"
    # r2c_prop not available for legacy track logs:
    incident=""; op_period=""; sar_id=""; uas=""; mid=""; rid=""; map_id=""; distance_mi=0.0
    r2c = prop.get('r2c_prop')
    if r2c:
        incident = r2c.get('incident', "")
        op_period = r2c.get('op_period', "")
        map_id = r2c.get('map_id', "")
        mid = r2c.get('mid', "")
        rid = r2c.get('rid', "")
        uas = r2c.get('model', "")
        distance_mi = float(r2c.get('distance_mi', 0.0))
        match = re.match(pattern, mid)
        if match:
            sar_id = match.group(1)
            uas = match.group(2)
    else:  # title should be available on legacy tracks: 
        title = prop.get('title')
        if not sar_id and title:
            match = re.match(pattern, title)
            if match:
                sar_id = match.group(1)
                uas = match.group(2)
        if not sar_id:
            sar_id = "unknown"
        if not uas:
            match = re.match("([^_]+)_.*", title)
            if match:
                uas = match.group(1)
            else:
                uas = rid
    return {'incident':incident, 'op_period':op_period, 'sar_id':sar_id,
            'uas':uas, 'mid':mid, 'rid':rid, 'map_id':map_id,
            'distance_mi':distance_mi}

def get_weather(lat, lon, utc_dt):
    """Parse a LineString 'properties' dictionary.
    Args:
        lat (float):       Coordinate lattitude
        lon (float):       Coordinate longitude
        utc_dt (datetime): Datetime @ UTC for given Coordinate.

    Returns:
        dictionary containing the following values:
            'temp' (float):    Hourly Temperature in degrees F.
            'hum' (float):     Hourly % humidity.
            'precip' (float):  Hourly inches of precip.
            'dew' (float):     Hourly dewpoint in degrees F.
            'wind' (float):    Hourly average windspeed in mph.
            'gusts' (float):   Hourly max windspeed in mph.
            'cloud' (float):   Hourly % cloud cover.
    """
    d_str = utc_dt.strftime("%Y-%m-%d")
    dt_str = utc_dt.strftime("%Y-%m-%dT%H:%M")
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&start_date={d_str}&end_date={d_str}&start_hour={dt_str}&end_hour={dt_str}&hourly=temperature_2m,relative_humidity_2m,dew_point_2m,precipitation,wind_speed_10m,wind_gusts_10m,cloud_cover&temperature_unit=fahrenheit&wind_speed_unit=mph&precipitation_unit=inch"
    try:
        res = requests.get(url).json()
        if not res or not 'hourly' in res:
            raise ValueError("open-meteo.com() missing expected response.")
        hourly = res['hourly']
        temp = hourly.get('temperature_2m',[0.0])[0]
        hum = hourly.get('relative_humidity_2m', [0.0])[0]
        dew = hourly.get('dew_point_2m', [0.0])[0]
        precip = hourly.get('precipitation',[0.0])[0]
        wind = hourly.get('wind_speed_10m', [0.0])[0]
        gusts = hourly.get('wind_gusts_10m', [0.0])[0]
        cloud = hourly.get('cloud_cover', [0.0])[0]
    except Exception as e:
        error_details = traceback.format_exc()
        logger.error(f"Exception in get_weather(): Failed to get weather "
                     f"for {lat},{lon}@UTC:{dt_str}\nurl:{url}\nres:{res}\n"
                     f"Details:{error_details}")
        temp=0.0; hum=0.0; precip=0.0; dew=0.0; wind=0.0; gusts=0.0; cloud=0.0

    return {"temp":temp, "hum":hum, "precip":precip, "dew":dew,
            "wind":wind, "gusts":gusts, "cloud":cloud}

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
        
    # Convert km to miles w/~5' resolution:
    return round(total_dist_km * 0.621371, 3)

async def get_api_key(header_value: str = Depends(api_key_header)):
    if header_value == TRACKER_API_KEY:
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
        geometry = f["geometry"]
        if not geometry or not (geometry["type"] == "LineString") or not geometry["coordinates"]: continue
        coordinate_list = geometry["coordinates"]

        for c in coordinate_list:
            if len(c) >= 4:
                timestamps.append(int(c[3]))
                if not coords_ref: coords_ref = (float(c[1]), float(c[0])) # lat, lon
    if not coords_ref: raise HTTPException(400, "No LineString coordinates found.")
    if not timestamps: raise HTTPException(400, "No LineString coordinate timestamps found.")
    if not prop: raise HTTPException(400, "No properties found.")

    start_ts_sec = int(min(timestamps) / 1000)
    # timestamps are always UTC since epoch, so let's 
    start_time = datetime.fromtimestamp(start_ts_sec)
    if int(start_time.strftime("%Y")) == 1970:
        raise HTTPException(400,
                            "Coordinate timestamps are likely straight from a UAS Remote ID msg."
                            "They need to be converted to current time by whatever tool is being "
                            "used to extract them before capturing to a geo-json file.")
    
    end_ts_sec = int(max(timestamps) / 1000)
    duration_sec = end_ts_sec - start_ts_sec
    if duration_sec < 60:
        return {"status": "Ignored",
                "reason": f"{duration_sec} second flight is too brief.\n"
                "Flights must be at least one minute long to register."
            }
    duration_hrs = round((end_ts_sec - start_ts_sec) / 3600.0, 3) # ~3.6 seconds resolution
    spec = parse_prop(prop)

    start_time_utc_naive = start_time.astimezone(tz=timezone.utc).replace(tzinfo=None)
    end_time = datetime.fromtimestamp(end_ts_sec)
    
    # Make sure flight doesn't overlap an existing flight (prevent duplicate submissions):
    db = SessionLocal()
    if not ALLOW_DUPLICATES:
        existing_overlap = db.query(Flight).filter(
            Flight.sar_id == spec['sar_id']
        ).filter(
            or_(
                # New start falls inside an existing flight
                and_(Flight.start_time <= start_time, Flight.end_time > start_time),
                # New end falls inside an existing flight
                and_(Flight.start_time < end_time, Flight.end_time >= end_time),
                # New flight completely swallows an existing flight
                and_(Flight.start_time >= start_time, Flight.end_time <= end_time)
            ) 
        ).first()
        if existing_overlap:
            db.close()
            raise HTTPException(
                status_code=409,
                detail=f"Conflict: This log overlaps with an existing entry for {spec['sar_id']}"
            )

    if spec['distance_mi']:
        distance = spec['distance_mi']  ;# faster if we have RID2Caltopo calculate this on the fly.
    else:
        distance = calculate_distance(coordinate_list)
    if distance < 0.1:
        return {"status": "Ignored",
                "reason": f"{distance} mi flight is too brief.\n"
                "Flights must be at least 0.1 mile to register."
            }
        
    timeofday_str = get_time_of_day(start_time_utc_naive, coords_ref[0], coords_ref[1])
    w = get_weather(coords_ref[0], coords_ref[1], start_time_utc_naive)

    new_flight = Flight(sar_id=spec['sar_id'], start_time=start_time, end_time=end_time, hours=duration_hrs,
                        incident=spec['incident'], op_period = spec['op_period'],
                        uas = spec['uas'], map_id = spec['map_id'], temp_f = w['temp'],
                        rhum_pct = w['hum'], dewpt_f = w['dew'], precip_in = w['precip'],
                        wind_mph = w['wind'], gusts_mph = w['gusts'],
                        cloudcvr_pct = w['cloud'], timeofday = timeofday_str,
                        distance_mi = distance)
    db.add(new_flight)
    db.commit()
    db.close()
    await manager.broadcast("refresh") # Tell everyone to reload
    return {"status": "Logged",
            "hours": duration_hrs,
            "timeofday": timeofday_str,
            "distance_mi": distance,
            "spec": spec,
            "weather": w
            }

@app.get("/", response_class=HTMLResponse)
async def public_dashboard(
        request: Request,
        response: Response,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None
):
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    db = SessionLocal()
#    db.query(Flight).delete()
#    db.commit()
#    db.close()
#    return {"status": "Db Reset"}
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
#    flights = query.order_by(Flight.start_time.desc()).all()
    flights = query.order_by(Flight.start_time.desc()).limit(50).all()
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
        new_sar_id: Annotated[str, Form()],
        new_uas: Annotated[str, Form()],
        user: str = Depends(check_admin)):
    db = SessionLocal()
    flight = db.query(Flight).filter(Flight.id == flight_id).first()
    if flight:
        flight.sar_id = new_sar_id.upper().strip()
        flight.uas = new_uas.strip()
        db.commit()
        db.close()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)

# delete a single flight:
@app.post("/admin/delete/{flight_id}")  # Must be .post
async def delete_flight(flight_id: int, user: str = Depends(check_admin)):
    db = SessionLocal()
    flight = db.query(Flight).filter(Flight.id == flight_id).first()
    if flight:
        db.delete(flight)
        db.commit()
        db.close()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)

# delete entire database:
@app.post("/admin/delete")  # Must be .post
async def delete_flight(user: str = Depends(check_admin)):
    db = SessionLocal()
    db.query(Flight).delete()
    db.commit()
    db.close()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)

# export timestamped .csv representation of the database:
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
    db.close()
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Flight", "Sar Id", "Date", "UAS", "Incident", "Op Period", "Map Id", "Hours", "Distance (mi)",
                     "Temp (F)", "Humidity (%)", "Dew Pt (F)", "Precip (in)", "Wind (mph)", "Gusts (mph)",
                     "Cloud Cover (%)", "Time Of Day"])
    
    for f in flights:
        writer.writerow([f.id, f.sar_id, f.start_time.strftime("%Y-%m-%d %H:%M:%S"),
                         f.uas, f.incident, f.op_period, f.map_id, f.hours, f.distance_mi,
                         f.temp_f, f.rhum_pct, f.dewpt_f, f.precip_in, f.wind_mph, f.gusts_mph,
                         f.cloudcvr_pct, f.timeofday])
    
    csv_content = output.getvalue()
    return Response(
        content=csv_content, 
        media_type="text/csv", 
        headers={"Content-Disposition": f"attachment; filename=r2c_audit{timestamp}.csv"}
    )

# Append new flights to the database.
# Pair with /export and /admin/delete for archive/restore functionality:
@app.post("/admin/import")
async def import_csv(file: UploadFile = File(...), user: str = Depends(check_admin)):
    content = await file.read()
    decoded = content.decode('utf-8')
    input_file = io.StringIO(decoded)
    reader = csv.DictReader(input_file)
    db = SessionLocal()
    try:
        for row in reader:
            # Create a new Flight object for each row
            new_flight = Flight(
                sar_id=row["Sar Id"],
                # Convert string back to datetime object
                start_time=datetime.strptime(row["Date"], "%Y-%m-%d %H:%M:%S"),
                uas=row["UAS"],
                incident=row["Incident"],
                op_period=row["Op Period"],
                map_id=row["Map Id"],
                hours=float(row["Hours"]) if row["Hours"] else 0.0,
                distance_mi=float(row["Distance (mi)"]) if row["Distance (mi)"] else 0.0,
                temp_f=float(row["Temp (F)"]) if row["Temp (F)"] else None,
                rhum_pct=float(row["Humidity (%)"]) if row["Humidity (%)"] else None,
                dewpt_f=float(row["Dew Pt (F)"]) if row["Dew Pt (F)"] else None,
                precip_in=float(row["Precip (in)"]) if row["Precip (in)"] else None,
                wind_mph=float(row["Wind (mph)"]) if row["Wind (mph)"] else None,
                gusts_mph=float(row["Gusts (mph)"]) if row["Gusts (mph)"] else None,
                cloudcvr_pct=float(row["Cloud Cover (%)"]) if row["Cloud Cover (%)"] else None,
                timeofday=row["Time Of Day"].lower()
            )
            db.add(new_flight)
        
        db.commit()
        retval = {"message": "Data imported successfully"}
    except Exception as e:
        db.rollback()
        retval = {"error": f"Import failed: {str(e)}"}
    finally:
        db.close()
    return retval

        
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text() # Keep connection alive
    except WebSocketDisconnect:
        manager.disconnect(websocket)
