# =-=-=-=-= IMPORTS =-=-=-=-=
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pyubx2 import UBXReader, UBX_PROTOCOL, NMEA_PROTOCOL
import math
import os
import sys
import time
import socket
import threading
import queue

# Enable CORS so local HTML file dashboard can talk to server safely
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

script_dir = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(script_dir, "logs") #LOGS_DIR is where the historical data is held
os.makedirs(LOGS_DIR, exist_ok=True)

# Mount static asset folders safely
for folder in ["css", "js", "assets"]:
    target = os.path.join(script_dir, folder)
    if os.path.exists(target):
        app.mount(f"/{folder}", StaticFiles(directory=target), name=folder)

# =-=-=-=-= THREADING AND QUEUE =-=-=-=-=
gnss_queue = queue.Queue()
live_data_buffer = []
buffer_lock = threading.Lock()

# GNSS reciever configuration (fix later so it is submitted by user?) TODO
TARGET_IP = "143.117.216.46"
TARGET_PORT = 5000

# =-=-=-=-= PARSE RECEIVER DATA =-=-=-=-=
# Parses pyubx2 objects (UBX or NMEA) into a unified dictionary format
def parse_ubx_data(raw_data):
    identity = getattr(raw_data, "identity", "")

    # trying to parse if it's in UBX Binary NAV-PVT
    if identity == "NAV-PVT":
        try:
            hour = getattr(raw_data, "hour", 0)
            minute = getattr(raw_data, "min", 0)
            sec = getattr(raw_data, "sec", 0)
            time_str = f"{hour:02d}:{minute:02d}:{sec:02d}"
            
            lat = raw_data.lat
            lon = raw_data.lon
            # height is in mm in UBX NAV-PVT
            alt = round(raw_data.height / 1000.0, 2) if hasattr(raw_data, "height") else 0.0

            return {
                "time": time_str,
                "latitude": round(lat, 7),
                "longitude": round(lon, 7),
                "altitude": alt
            }
        except AttributeError:
            return None

    # trying to parse if it's in UBX Binary NAV-POSLLH
    elif identity == "NAV-POSLLH":
        try:
            itow = getattr(raw_data, "iTOW", 0)
            lat = raw_data.lat
            lon = raw_data.lon
            alt = round(raw_data.hMSL / 1000.0, 2) if hasattr(raw_data, "hMSL") else 0.0

            return {
                "time": f"iTOW:{itow}",
                "latitude": round(lat, 7),
                "longitude": round(lon, 7),
                "altitude": alt
            }
        except AttributeError:
            return None

    # fallback if NMEA Sentences (GGA / RMC)
    elif "GGA" in identity or "RMC" in identity:
        try:
            lat = getattr(raw_data, "lat", None)
            lon = getattr(raw_data, "lon", None)
            
            # Skip invalid or empty fixes
            if lat is None or lon is None or lat == "" or lon == "":
                return None

            time_val = getattr(raw_data, "time", "00:00:00")
            alt_val = getattr(raw_data, "alt", 0.0)

            return {
                "time": str(time_val),
                "latitude": round(float(lat), 7),
                "longitude": round(float(lon), 7),
                "altitude": round(float(alt_val), 2)
            }
        except (AttributeError, ValueError):
            return None

    # Ignore satellite/DOP metadata
    return None


# =-=-=-=-= THREAD 1: RECEIVER STREAM READER =-=-=-=-=
def gnss_socket_thread(host: str, port: int):
    print(f"[THREAD 1] Socket Worker Started. Connecting to {host}:{port}...")

    while True:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10.0)
            sock.connect((host, port))
            print(f"[THREAD 1] Successfully connected to {host}:{port}")

            stream = sock.makefile('rb')
            
            ubr = UBXReader(stream, protfilter=UBX_PROTOCOL | NMEA_PROTOCOL) # Enable both UBX and NMEA protocol parsing

            for (raw_bytes, parsed_data) in ubr:
                if parsed_data:
                    parsed_point = parse_ubx_data(parsed_data)
                    if parsed_point:
                        gnss_queue.put(parsed_point)

        except Exception as e:
            print(f"[THREAD 1 WARNING] Connection error ({e}). Retrying in 5s...")
            time.sleep(5)


# =-=-=-=-= THREAD 2: DATA PROCESSOR & LOGGING =-=-=-=-=
# 1. pulls coordinates off the queue
# 2. prints the debug information (latitude, longitude, altitude, time)
# 3. appends records to a single master file
def data_processor_thread():
    print("[THREAD 2] Consumer Processor Started.")
    master_log_filepath = os.path.join(LOGS_DIR, "gnss_master_log.csv")

    while True:
        try:
            item = gnss_queue.get(timeout=1.0) #block until data is avaliable in the queue
            
            # Append to single master log csv file
            file_exists = os.path.exists(master_log_filepath)
            with open(master_log_filepath, "a") as f:
                if not file_exists:
                    f.write("timestamp,latitude,longitude,altitude\n")
                f.write(f"{item['time']},{item['latitude']},{item['longitude']},{item['altitude']}\n")

            # Store in live buffer for updating the dashboard ui live
            with buffer_lock:
                item['id'] = len(live_data_buffer)
                live_data_buffer.append(item)

            gnss_queue.task_done()

            print(f"[SUCCESS] Parsed & Logged -> Time: {item['time']} | Lat: {item['latitude']} | Lon: {item['longitude']} | Alt: {item['altitude']}m")

        except queue.Empty:
            continue
        except Exception as e:
            print(f"[ERROR] Failed to save or process frame: {e}")


class IPConfig(BaseModel):
    ip_1: str
    ip_2: str
    ip_3: str


def calculate_distance(latA, lonA, latB, lonB):
    delta_lat_m = (latA - latB) * 111120
    delta_lon_m = (lonA - lonB) * 65315  #assumes 54'N latitude, fix to be dynamic? TODO
    return math.sqrt(delta_lat_m**2 + delta_lon_m**2)


# Start background receiver and consumer threads
t1 = threading.Thread(target=gnss_socket_thread, args=(TARGET_IP, TARGET_PORT), daemon=True)
t2 = threading.Thread(target=data_processor_thread, daemon=True)
t1.start()
t2.start()


# =-=-=-=-= APIs =-=-=-=-=
@app.post("/api/start_tracking")
def start_tracking(config: IPConfig):
    print(f"\n[SYSTEM] Received IP addresses: {config.ip_1}, {config.ip_2}, {config.ip_3}")
    
    # TODO connect pyubx2/socket here
    
    lat1, lon1 = 54.5822, -5.9371  
    lat2, lon2 = 54.5822, -5.9371 # Simulating an attack (same location)
    lat3, lon3 = 54.5825, -5.9375  # Simulating safe (different location)

    # Calculate distance betwwen all antennas
    dist_1_2 = calculate_distance(lat1, lon1, lat2, lon2)
    dist_1_3 = calculate_distance(lat1, lon1, lat3, lon3)
    dist_2_3 = calculate_distance(lat2, lon2, lat3, lon3)

    # Evaluate against the 5-meter geofence
    geofence = 5.0 #can change to be more strict e.g. might change to 4.88? TODO
    if dist_1_2 < geofence or dist_1_3 < geofence or dist_2_3 < geofence:
        system_status = "WARNING: Spoofing Detected! Multiple antennas are reporting the same location."
    else:
        system_status = "NORMAL: All antennas are separated."

    # Return the status to Card 2 on the frontend web
    return {"status": system_status, "distance12": dist_1_2, "distance13": dist_1_3, "distance23": dist_2_3}


@app.get("/api/logs/history")
def get_historical_logs():
    #Scans ./logs folder and loads all historical points on app launch
    historical_points = []
    log_files = sorted([f for f in os.listdir(LOGS_DIR) if f.endswith('.csv')])
    for log_file in log_files:
        filepath = os.path.join(LOGS_DIR, log_file)
        with open(filepath, 'r') as f:
            lines = f.readlines()[1:]
            for line in lines:
                parts = line.strip().split(',')
                if len(parts) == 4:
                    historical_points.append({
                        "time": parts[0],
                        "latitude": float(parts[1]),
                        "longitude": float(parts[2]),
                        "altitude": float(parts[3])
                    })
                    
    return JSONResponse(content={"count": len(historical_points), "data": historical_points})


@app.get("/api/logs/live")
#Fetches ONLY new data points recorded since the given index
def get_live_updates(since: int = 0):
    with buffer_lock:
        new_points = live_data_buffer[since:]
        next_id = len(live_data_buffer)
        
    return JSONResponse(content={"next_index": next_id, "data": new_points})


@app.get("/")
@app.get("/index.html")
def get_dashboard():
    return FileResponse(os.path.join(script_dir, "index.html"))

# =-=-=-=-= # MAIN =-=-=-=-=
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)