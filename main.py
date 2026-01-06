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
import secrets
import numpy as np
from pprint import pprint
from datetime import datetime, date, timedelta, timezone, UTC
from zoneinfo import ZoneInfo
from timezonefinder import TimezoneFinder
from typing import Optional, Annotated
from contextlib import asynccontextmanager

from fastapi import Security, Depends, FastAPI, Request, HTTPException, Query, Form
from fastapi import status, Response, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.security.api_key import APIKeyHeader
from starlette.status import HTTP_403_FORBIDDEN
from starlette.middleware.sessions import SessionMiddleware

from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, MetaData, Table, select, text
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, desc, func, or_, and_
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import sessionmaker, Session
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
SECRET_KEY = os.environ.get("SECRET_KEY", False)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
filepath = __file__
filedate = datetime.fromtimestamp(os.path.getmtime(filepath))
print(f"{filepath} version is {filedate}")

# connector no longer needed when running locally in same VM.
def getconn():
    connector = Connector()
    return connector.connect(DB_URL, "pg8000", user=DB_USER, password=DB_PASS, db=DB_NAME, ip_type=IPTypes.PUBLIC)
# engine = create_engine("postgresql+pg8000://", creator=getconn)
# engine = create_engine(DB_URL)
# SessionLocal = sessionmaker(autocommit= False, autoflush=False, bind=engine)
# Base.metadata.create_all(bind=engine)

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
    start_lat = Column(Float, default = 0.0)
    start_lng = Column(Float, default = 0.0)
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

engine = create_async_engine(DB_URL, echo=True)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False
)

if __name__ == "__main__":
    asyncio.run(init_db())
    
@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup Create tables
    await init_db()
    yield
    # Shutdown Clean up resources (if needed)
    await engine.dispose()

app = FastAPI(lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY
)

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
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
        
security = HTTPBasic()
def check_admin(credentials: HTTPBasicCredentials = Depends(security)):
    if (not secrets.compare_digest(credentials.username, TRACKER_ADMIN_USER)
        or not secrets.compare_digest(credentials.password, TRACKER_ADMIN_PASS)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"}
        )
    return credentials.username

def get_time_of_day(start_ts_sec, lat, lng):
    utc_time = datetime.fromtimestamp(start_ts_sec, tz=timezone.utc)
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=RuntimeWarning)
        # suppress warnings from suncalc:
        sun_times = get_times(utc_time, lng, lat)
    # Sort keys by time to determine the active range
    # We filter for common phases returned by suncalc-py
    phases = [
        (sun_times['night_end'].timestamp(), "Pre-dawn"),
        (sun_times['nautical_dawn'].timestamp(), "Nautical Dawn"),
        (sun_times['dawn'].timestamp(), "Civil Dawn"),
        (sun_times['sunrise'].timestamp(), "Sunrise"),
        (sun_times['sunrise_end'].timestamp(), "Early Morning"),
        (sun_times['golden_hour_end'].timestamp(), "Morning"),
        (sun_times['solar_noon'].timestamp(), "Afternoon"),
        (sun_times['golden_hour'].timestamp(), "Golden Hour"),
        (sun_times['sunset_start'].timestamp(), "Sunset"),
        (sun_times['sunset'].timestamp(), "Civil Dusk"),
        (sun_times['dusk'].timestamp(), "Nautical Dusk"),
        (sun_times['nautical_dusk'].timestamp(), "Nautical Dusk"),
        (sun_times['night'].timestamp(), "Night")
    ]
    
    
    phases.sort(key=lambda x: x[0]) ;# Sort by the datetime value
    timeofday_str = "Night"  # Default for times before the first phase (early AM)
    for phase_time, description in phases:
        if start_ts_sec >= phase_time:
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

def get_weather(ts_sec, lat, lon):
    """Parse a LineString 'properties' dictionary.
    Args:
        ts_sec (int):      UTC timestamp for given Coordinate.
        lat (float):       Coordinate lattitude
        lon (float):       Coordinate longitude

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
    utc_dt = datetime.fromtimestamp(ts_sec, tz=timezone.utc)
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

def filter_outlier_coords(coords):
    """ Process a list of geojson coordinates to remove any outliers.
        Some remote id modules (ahem... Autel) do not care if they spit out 
        garbage coords, so try to filter the worst offenders out.  The returned
        coordinate array omits altitude and timestamp fields but will otherwise
        sequentially match the input array with any outliers tossed out.

    Args:
        coords [[
            lon (float),
            lat (float),
            alt (float),
            ts  (int)
         ]]
    
    Returns:
        coords [[
            lat (float),
            lon (float)
        ]]
    """
    # Use Interquartile Range method to compute avg lat,lon sans any outliers:
    lat_list = []; lon_list = []
    for i in range(len(coords)):
      lat_list.append(float(coords[i][1]))
      lon_list.append(float(coords[i][0]))
    min_lat = np.percentile(lat_list, 2)
    max_lat = np.percentile(lat_list, 98)
    iqr_lat = max_lat - min_lat
    min_lon = np.percentile(lon_list, 2)
    max_lon = np.percentile(lon_list, 98)
    iqr_lon = max_lon - min_lon
    lower_lat = math.fabs(min_lat) - 1.5 * iqr_lat
    upper_lat = math.fabs(max_lat) + 1.5 * iqr_lat
    lower_lon = math.fabs(min_lon) - 1.5 * iqr_lon
    upper_lon = math.fabs(max_lon) + 1.5 * iqr_lon
    results = []
    for i in range(len(lat_list)):
        lat = lat_list[i]; lon = lon_list[i] 
        if (lower_lat <= math.fabs(lat) <= upper_lat) and (lower_lon <= math.fabs(lon) <= upper_lon):
            results.append([lat,lon])
        else:
            latpfx = ""; lonpfx = ""
            if lat < 0:
                latpfx = "-" 
            if lon < 0:
                lonpfx = "-"
            print(f"XYZZY: ignoring: {i}:!{lower_lat}<={lat}<={upper_lat},{lower_lon}<={lon}<={upper_lon}")
    return results

def compute_distance(coords):
    """ Process/filter geojson coords.
    Args:
        coords [[
            lat (float),
            lon (float)
         ]]
    
    Returns
          distance_mi (float)
    """
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

def format_datetime(value):
    if value is None:
        return ""
    return value.strftime('%d%b%y@%H:%M:%S-%Z')

def datetime_from_format(fmtstr):
    return datetime.strptime(fmtstr, '%d%b%y@%H:%M:%S-%Z')

def to_iso_naive(dt):
    if dt is None:
        return ""
    return dt.isoformat()

def get_flashed_messages(request: Request):
    return request.session.pop("_messages") if "_messages" in request.session else []

templates.env.globals.update(get_flashed_messages=get_flashed_messages)

def flash(request: Request, message: str, category: str = "info"):
    if "_messages" not in request.session:
        request.session["_messages"] = []
    request.session["_messages"].append({"message": message, "category": category})


TF = TimezoneFinder()
def localize_flight_time(dt, lat, lng):
    if not dt or lat is None or lng is None:
        return dt

    dt_utc = dt.replace(tzinfo=timezone.utc)

    # Returns a string like 'America/Los_Angeles' or None
    tz_name = TF.timezone_at(lat=lat, lng=lng)

    # Convert to the local timezone
    if tz_name:
        local_dt = dt_utc.astimezone(ZoneInfo(tz_name))
    return local_dt


templates.env.filters["localize_flight_time"] = localize_flight_time
templates.env.filters["fmt_datetime"] = format_datetime

# --- ROUTES ---
@app.put("/upload")
async def upload(
        request: Request,
        db: AsyncSession = Depends(get_db)):
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
        
    # Extract start time, and coordinates from GeoJSON:
    start_ts, end_ts = None, None
    for f in data.get("features", []):
        if start_ts: raise HTTPException(400, "Only one track supported per log file.")
        prop = f["properties"]
        geometry = f["geometry"]
        if not geometry or not (geometry["type"] == "LineString") or not geometry["coordinates"]: continue
        coordinate_list = geometry["coordinates"]

        for c in coordinate_list:
            if len(c) >= 4:
                if not start_ts:
                    start_ts = int(c[3])    # timestamp
                end_ts = int(c[3])
 
    if not start_ts: raise HTTPException(400, "No LineString coordinate timestamps found.")
    if not prop: raise HTTPException(400, "No properties found.")
    
    # N.B. timestamps are always UTC since epoch
    start_ts_sec = round(start_ts / 1000.0, 2)
    end_ts_sec = round(end_ts / 1000.0, 2)
    duration_sec = end_ts_sec - start_ts_sec
    if duration_sec < 60:
        return {"status": "Ignored",
                "reason": f"{duration_sec} second flight is too brief.\n"
                "Flights must be at least one minute long to record."
            }
    duration_hrs = round((end_ts_sec - start_ts_sec) / 3600.0, 3) # ~3.6 seconds resolution

    spec = parse_prop(prop)
    distance = spec.get('distance_mi') # faster if we let RID2Caltopo calculate this on the fly.
    title=prop.get('title', 'no_title_in_properties')

    coords = filter_outlier_coords(coordinate_list)
    if not coords:
        raise HTTPException(
            status_code=409,
            detail=f"No valid coordinates in {title}"
        )
        
    start_lat, start_lng = coords[0][0], coords[0][1]
    distance_me = compute_distance(coords)
    filter_count = len(coordinate_list) - len(coords)
    if filter_count > 0:
        print(f"XYZZY: ignoring {filter_count} outlier coordinates from {title}.   Start:{start_lat},{start_lng}")

    
    # FIXME: If RID2Caltopo provided distance, compare the two - until we regularly get consensus
    if not distance:
        distance = distance_me
    elif math.fabs(distance - distance_me) > 0.01:
        print(f"XYZZY: Supplied distance:{distance} vs calculated:{distance_me}.  Using calculated for {title}")
        distance = distance_me

    if distance < 0.1:
        return {"status": "Ignored",
                "reason": f"{distance} mi flight is too brief.\n"
                "Flights must be at least 0.1 mile to register."
            }

    start_time = datetime.fromtimestamp(start_ts_sec, tz=timezone.utc).replace(tzinfo=None)
    end_time = datetime.fromtimestamp(end_ts_sec, tz=timezone.utc).replace(tzinfo=None)
    if int(start_time.strftime("%Y")) == 1970:
        raise HTTPException(400,
                            "Coordinate timestamps are likely straight from a UAS Remote ID msg."
                            "They need to be converted to current time by whatever tool is being "
                            "used to extract them before capturing to a geo-json file.")
    
    
    # Make sure flight doesn't overlap an existing flight (prevent duplicate submissions):
    stmt = select(Flight).filter(Flight.sar_id == spec['sar_id']).filter(
        or_(
            # New start falls inside an existing flight
            and_(Flight.start_time <= start_time, Flight.end_time > start_time),
            # New end falls inside an existing flight
            and_(Flight.start_time < end_time, Flight.end_time >= end_time),
            # New flight completely swallows an existing flight
            and_(Flight.start_time >= start_time, Flight.end_time <= end_time)
        ) 
    )
    result = await db.execute(stmt)

    # While we're waiting for db fetch to complete do time consuming things:
    timeofday_str = get_time_of_day(start_ts_sec, start_lat, start_lng)
    w = get_weather(start_ts_sec, start_lat, start_lng)

    # Now catch completed fetch:
    existing = result.scalars().first()
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Conflict: This log overlaps with existing entry {existing.id} for {spec['sar_id']}"
        )

    new_flight = Flight(sar_id=spec['sar_id'].upper(), start_time=start_time, end_time=end_time, hours=duration_hrs,
                        start_lat=start_lat, start_lng=start_lng,
                        incident=spec['incident'], op_period = spec['op_period'],
                        uas = spec['uas'].lower(), map_id = spec['map_id'].upper(), temp_f = w['temp'],
                        rhum_pct = w['hum'], dewpt_f = w['dew'], precip_in = w['precip'],
                        wind_mph = w['wind'], gusts_mph = w['gusts'],
                        cloudcvr_pct = w['cloud'], timeofday = timeofday_str,
                        distance_mi = distance)
    db.add(new_flight)
    await db.commit()
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
        db: AsyncSession = Depends(get_db),
        start_date: Optional[date] = None,
        end_date: Optional[date] = None):
    response.headers["X-Robots-Tag"] = "noindex, nofollow"

    # Base query:
    stmt = select(Flight)
    if start_date:
        start_dt = datetime.combine(start_date, datetime.min.time())
        stmt = stmt.where(Flight.start_time >= start_dt)
    if end_date:
        end_dt = datetime.combine(end_date, datetime.max.time())
        stmt = stmt.where(Flight.start_time <= end_dt)

    # Group by pilot, sum hours
    subq_totals = stmt.with_only_columns(
        Flight.sar_id,
        func.sum(Flight.hours).label("total_hours"),
        func.sum(Flight.distance_mi).label("total_miles"),
        func.max(Flight.start_time).label("last_active")
    ).group_by(Flight.sar_id).subquery()

    # Window subquery to find the single latest record per sar_id
    # This is how we get lt/lng without joining or grouping complications
    latest_flights_subq = stmt.with_only_columns(
        Flight.sar_id,
        Flight.start_lat,
        Flight.start_lng,
        func.row_number().over(
            partition_by=Flight.sar_id,
            order_by=Flight.start_time.desc()
        ).label("rn")
    ).subquery()

    leaderboard_stmt = select(
        subq_totals.c.sar_id,        
        subq_totals.c.total_hours,
        subq_totals.c.total_miles,
        subq_totals.c.last_active,
        latest_flights_subq.c.start_lat,
        latest_flights_subq.c.start_lng
    ).join(
        latest_flights_subq,
        (subq_totals.c.sar_id == latest_flights_subq.c.sar_id) & (latest_flights_subq.c.rn == 1)
    ).order_by(desc(subq_totals.c.total_hours)).limit(10)
    leaderboard_result = await db.execute(leaderboard_stmt)
    leaderboard = leaderboard_result.all()

    flights_stmt = stmt.order_by(Flight.start_time.desc()).limit(50)
    flights_result = await db.execute(flights_stmt)
    flights = flights_result.scalars().all()

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "flights": flights,
        "timezone" : ZoneInfo("America/Los_Angeles"),
        "leaderboard": leaderboard,
        "start_date" : start_date,
        "end_date" : end_date
    })

# List the admin page
@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(
        request: Request,
        db: AsyncSession = Depends(get_db),
        user: str = Depends(check_admin),
        start_date: Optional[date] = None,
        end_date: Optional[date] = None):

    # Base query:
    stmt = select(Flight)
    if start_date:
        start_dt = datetime.combine(start_date, datetime.min.time())
        stmt = stmt.where(Flight.start_time >= start_dt)
    if end_date:
        end_dt = datetime.combine(end_date, datetime.max.time())
        stmt = stmt.where(Flight.start_time <= end_dt)
    stmt = stmt.order_by(Flight.start_time.desc()).limit(50)
    result = await db.execute(stmt)
    flights = result.scalars().all()
    return templates.TemplateResponse("admin.html", {"request": request, "flights": flights})

@app.post("/admin/edit/{flight_id}")
async def edit_flight(
        request: Request,
        flight_id: int,
        new_sar_id: Annotated[str, Form()],
        new_uas: Annotated[str, Form()],
        db: AsyncSession = Depends(get_db),
        user: str = Depends(check_admin)):

    result = await db.execute(select(Flight).filter(Flight.id == flight_id))
    flight = result.scalar_one_or_none()
    if not flight:
        return {"error": f"Flight {flight_id} undefined"}
    flight.sar_id = new_sar_id.upper().strip()
    flight.uas = new_uas.lower().strip()
    await db.commit()
    flash(request, f"Flight {flight_id} successfully edited", "success")
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)

# delete a single flight:
@app.post("/admin/delete/{flight_id}")  # Must be .post
async def delete_flight(
        request: Request,
        flight_id: int,
        db: AsyncSession = Depends(get_db),
        user: str = Depends(check_admin)):

    result = await db.execute(select(Flight).filter(Flight.id == flight_id))
    flight = result.scalar_one_or_none()
    if not flight:
        flash(request, f"Flight {flight_id} not found", "warning")
        return RedirectResponse(url="/admin?error=not_found", status_code=status.HTTP_303_SEE_OTHER)
    
    await db.delete(flight)
    await db.commit()
    flash(request, f"Flight {flight_id} deleted successfully", "success")
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)

# delete entire database:
@app.post("/admin/delete")  # Must be .post
async def reset_table(
        request: Request,
        user: str = Depends(check_admin),
        db: AsyncSession = Depends(get_db)):
    
    await db.execute(text("TRUNCATE TABLE flights RESTART IDENTITY CASCADE;"))
    await db.commit()
    flash(request, f"flights table successfully cleaned.", "success")
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
        end_date: Optional[date] = None,
        db: AsyncSession = Depends(get_db)):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Base query:
    stmt = select(Flight)
    if start_date:
        start_dt = datetime.combine(start_date, datetime.min.time())
        stmt = stmt.where(Flight.start_time >= start_dt)
    if end_date:
        end_dt = datetime.combine(end_date, datetime.max.time())
        stmt = stmt.where(Flight.start_time <= end_dt)

    stmt = stmt.order_by(Flight.start_time)

    result = await db.execute(stmt)
    flights = result.scalars().all()

    
    output = io.StringIO()
    writer = csv.writer(output)
    # N.B. keys need to match those used in import:
    writer.writerow(["Flight", "Sar Id", "UAS", "Incident", "Op Period", "Map Id", "Start Time", "End Time",
                     "Start Lattitude", "Start Longitude", "Hours", "Distance (mi)",
                     "Temp (F)", "Rel Humidity (%)", "Dew Pt (F)", "Precip (in)", "Wind (mph)", "Gusts (mph)",
                     "Cloud Cover (%)", "Time Of Day"])
    
    for f in flights:
        writer.writerow([f.id, f.sar_id.upper(), f.uas.lower(), f.incident, f.op_period, f.map_id.upper(),
                         format_datetime(f.start_time.replace(tzinfo=UTC)),
                         format_datetime(f.end_time.replace(tzinfo=UTC)),
                         f.start_lat, f.start_lng, f.hours, f.distance_mi, f.temp_f,
                         f.rhum_pct, f.dewpt_f, f.precip_in, f.wind_mph, f.gusts_mph,
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
async def import_csv(
        file: UploadFile = File(...),
        db: AsyncSession = Depends(get_db),
        user: str = Depends(check_admin)):
    content = await file.read()
    decoded = content.decode('utf-8')
    input_file = io.StringIO(decoded)
    reader = csv.DictReader(input_file)
    retval = {"error": "unspecified"}
    try:
        for row in reader:
            # N.B. keys need to match those used in export.
            # Create a new Flight object for each row
            new_flight = Flight(
                sar_id=row.get('Sar Id', '').upper(),
                uas=row.get('UAS', '').lower(),
                incident=row.get('Incident', ''),
                op_period=row.get('Op Period', ''),
                map_id=row.get('Map Id', ''),
                start_time=datetime_from_format(row.get('Start Time', None)),
                end_time=datetime_from_format(row.get('End Time', None)),
                start_lat=float(row.get('Start Lattitude', 0.0)),
                start_lng=float(row.get('Start Longitude', 0.0)),
                hours=float(row.get('Hours', 0.0)),
                distance_mi=float(row.get('Distance (mi)', 0.0)),
                temp_f=float(row.get('Temp (F)', 0.0)),
                rhum_pct=float(row.get('Rel Humidity (%)', 0.0)),
                dewpt_f=float(row.get('Dew Pt (F)', 0.0)),
                precip_in=float(row.get('Precip (in)', 0.0)),
                wind_mph=float(row.get('Wind (mph)', 0.0)),
                gusts_mph=float(row.get('Gusts (mph)', 0.0)),
                cloudcvr_pct=float(row.get('Cloud Cover (%)', 0.0)),
                timeofday=row.get('Time Of Day', "")
            )
            db.add(new_flight)
        
        await db.commit()
        return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)
    except Exception as e:
        db.rollback()
        retval = {"error": f"Import failed: {str(e)}"}

    return retval

        
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text() # Keep connection alive
    except WebSocketDisconnect:
        manager.disconnect(websocket)
