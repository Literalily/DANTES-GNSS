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
import datetime
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

# GNSS receiver configuration to track multiple receivers at once
# every point recorded is tagged with the "host:port" it came from so the dashboard can tell the receivers apart
RECEIVERS = [
    {"host": "143.117.216.46", "port": 5000},
    {"host": "143.117.216.46", "port": 5001},
    {"host": "143.117.216.46", "port": 5002}
]
 
# System and Connection Status Tracking - one entry per receiver
connection_status = {
    f"{r['host']}:{r['port']}": {
        "is_connected": False,
        "error_message": None,
        "last_packet_time": 0
    }
    for r in RECEIVERS
}

# =-=-=-=-= PARSE RECEIVER DATA =-=-=-=-=
# Parses pyubx2 objects (UBX or NMEA) into a unified dictionary format
def parse_ubx_data(raw_data, receiver_id):
    identity = getattr(raw_data, "identity", "")

    # trying to parse if it's in UBX Binary NAV-PVT
    if identity == "NAV-PVT":
        try:
            hour = getattr(raw_data, "hour", 0)
            minute = getattr(raw_data, "min", 0)
            sec = getattr(raw_data, "sec", 0)
            year = getattr(raw_data, "year", None)
            month = getattr(raw_data, "month", None)
            day = getattr(raw_data, "day", None)

            if year and year > 2000 and month and day:
                date_str = f"{year:04d}-{month:02d}-{day:02d}"
            else:
                date_str = datetime.date.today().strftime("%Y-%m-%d")

            datetime_str = f"{date_str} {hour:02d}:{minute:02d}:{sec:02d}"

            lat = raw_data.lat
            lon = raw_data.lon
            # alt = raw_data.height #TODO LILY DELETE
            alt = round(getattr(raw_data, "hMSL", 0) / 1000.0, 2)

            return {
                "time": datetime_str, 
                "latitude": round(lat, 7),
                "longitude": round(lon, 7),
                "altitude": alt,
                "receiver": receiver_id
            }
        except AttributeError:
            return None

    # trying to parse if it's in UBX Binary NAV-POSLLH
    elif identity == "NAV-POSLLH":
        try:
            date_str = datetime.date.today().strftime("%Y-%m-%d")
            itow = getattr(raw_data, "iTOW", 0)
            lat = raw_data.lat
            lon = raw_data.lon
            alt = round(raw_data.hMSL / 1000.0, 2) if hasattr(raw_data, "hMSL") else 0.0

            return {
                "time": f"{date_str} (iTOW:{itow})",
                "latitude": round(lat, 7),
                "longitude": round(lon, 7),
                "altitude": alt,
                "receiver": receiver_id
            }
        except AttributeError:
            return None

    # fallback if NMEA Sentences (GGA / RMC)
    elif "GGA" in identity or "RMC" in identity:
        try:
            lat = getattr(raw_data, "lat", None)
            lon = getattr(raw_data, "lon", None)
            if lat is None or lon is None or lat == "" or lon == "": # Skip invalid or empty fixes
                return None

            time_val = getattr(raw_data, "time", "00:00:00")
            date_str = datetime.date.today().strftime("%Y-%m-%d")
            datetime_str = f"{date_str} {time_val}"
            alt_val = getattr(raw_data, "alt", 0.0)

            return {
                "time": datetime_str,
                "latitude": round(float(lat), 7),
                "longitude": round(float(lon), 7),
                "altitude": round(float(alt_val), 2),
                "receiver": receiver_id
            }
        except (AttributeError, ValueError):
            return None

    # Ignore satellite/DOP metadata
    return None

# =-=-=-=-= THREAD 1: RECEIVER STREAM READER =-=-=-=-=
def gnss_socket_thread(host: str, port: int):
    receiver_id = f"{host}:{port}"
    print(f"[{receiver_id}] Socket Worker Started. Connecting...")
 
    while True:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10.0)  # Set 10-second socket read timeout
            sock.connect((host, port))
 
            connection_status[receiver_id]["is_connected"] = True
            connection_status[receiver_id]["error_message"] = None

            print(f"[{receiver_id}] Successfully connected.")
 
            stream = sock.makefile('rb')
            ubr = UBXReader(stream, protfilter=UBX_PROTOCOL | NMEA_PROTOCOL) # Enable both UBX and NMEA protocol parsing
 
            packets_in_session = 0
            session_start = time.time()
 
            for (raw_bytes, parsed_data) in ubr:
                if parsed_data:
                    parsed_point = parse_ubx_data(parsed_data, receiver_id)
                    if parsed_point:
                        packets_in_session += 1
                        connection_status[receiver_id]["last_packet_time"] = time.time()
                        connection_status[receiver_id]["error_message"] = None
                        gnss_queue.put(parsed_point)

                # Timeout check: If connected for 10 seconds without receiving position data
                if packets_in_session == 0 and (time.time() - session_start) > 10.0:
                    raise TimeoutError("Socket connected but no GNSS data packets received.")
 
        except (socket.timeout, TimeoutError, ConnectionRefusedError, OSError) as e:
            err_msg = (
                f"[CRITICAL ERROR] Receiver port blocked or unavailable ({receiver_id}). "
                f"Another application (e.g., u-Center) may be open and locking the receiver port."
            )
            print(f"\n{'='*80}\n{err_msg}\n{'='*80}\n")
 
            connection_status[receiver_id]["is_connected"] = False
            connection_status[receiver_id]["error_message"] = (
                f"CRITICAL ERROR: Unable to access receiver at {receiver_id}. "
                f"u-Center or another program is open and locking the connection port."
            )
 
        except Exception as e:
            print(f"[{receiver_id} WARNING] Connection error: {e}")
 
        finally:
            try:
                sock.close()
            except:
                pass
 
        # sleep 10 seconds between retries to avoid cmd spam
        time.sleep(10)


# =-=-=-=-= THREAD 2: DATA PROCESSOR & LOGGING =-=-=-=-=
# 1. pulls coordinates off the queue
# 2. prints the debug information (latitude, longitude, altitude, time, receiver)
# 3. appends records to a single master file
def data_processor_thread():
    print("[THREAD 2] Consumer Processor Started.")
    master_log_filepath = os.path.join(LOGS_DIR, "gnss_master_log.csv")

    while True:
        try:
            item = gnss_queue.get(timeout=1.0) #block until data is avaliable in the queue
            
            file_exists = os.path.exists(master_log_filepath)  # Append to single master log csv file
            with open(master_log_filepath, "a") as f:
                if not file_exists:
                    f.write("datetime,latitude,longitude,altitude,receiver\n")
                f.write(f"{item['time']},{item['latitude']},{item['longitude']},{item['altitude']},{item['receiver']}\n")

            # Store in live buffer for updating the dashboard ui live
            with buffer_lock:
                item['id'] = len(live_data_buffer)
                live_data_buffer.append(item)

            gnss_queue.task_done()
            print(f"[SUCCESS] Saved -> Datetime: {item['time']} | Lat: {item['latitude']} | Lon: {item['longitude']} | Alt: {item['altitude']}m | Receiver: {item['receiver']}")

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


# Start one background receiver thread per configured receiver, plus the logger
for r in RECEIVERS:
    threading.Thread(target=gnss_socket_thread, args=(r["host"], r["port"]), daemon=True).start()
threading.Thread(target=data_processor_thread, daemon=True).start()


# =-=-=-=-= APIs =-=-=-=-=
@app.post("/api/start_tracking")
def start_tracking(config: IPConfig):
    lat1, lon1 = 54.5822, -5.9371
    lat2, lon2 = 54.5822, -5.9371 # Simulating an attack (same location)
    lat3, lon3 = 54.5825, -5.9375  # Simulating safe (different location)

    # Calculate distance betwwen all antennas
    dist_1_2 = calculate_distance(lat1, lon1, lat2, lon2)
    dist_1_3 = calculate_distance(lat1, lon1, lat3, lon3)
    dist_2_3 = calculate_distance(lat2, lon2, lat3, lon3)

    geofence = 5.0  #can change to be more strict e.g. might change to 4.88? TODO
    if dist_1_2 < geofence or dist_1_3 < geofence or dist_2_3 < geofence: # Evaluate against the 5-meter geofence
        system_status = "WARNING: Spoofing Detected! Multiple antennas are reporting the same location."
    else:
        system_status = "NORMAL: All antennas are separated."

    # Return the status to Card 2 on the frontend web
    return {
        "status": system_status, 
        "distance12": dist_1_2, 
        "distance13": dist_1_3, 
        "distance23": dist_2_3
    }


@app.get("/api/logs/history")
def get_historical_logs(max_points: int = 3000):
    # Scans ./logs folder and loads all historical points on app launch.
    # If there are more rows than max_points, the data is evenly thinned
    # out (always keeping the most recent point) so the chart stays fast
    # and readable instead of trying to plot every single raw sample.
    historical_points = []
    log_files = sorted([f for f in os.listdir(LOGS_DIR) if f.endswith('.csv')])
    for log_file in log_files:
        filepath = os.path.join(LOGS_DIR, log_file)
        with open(filepath, 'r') as f:
            lines = f.readlines()[1:]
            for line in lines:
                parts = line.strip().split(',')
                if len(parts) == 5:
                    historical_points.append({
                        "time": parts[0],
                        "latitude": float(parts[1]),
                        "longitude": float(parts[2]),
                        "altitude": float(parts[3]),
                        "receiver": parts[4]
                    })
 
    total = len(historical_points)
    if total > max_points:
        step = math.ceil(total / max_points)
        thinned = historical_points[::step]
        if historical_points and thinned[-1] is not historical_points[-1]:
            thinned.append(historical_points[-1])  # always keep the most recent point
        historical_points = thinned
 
    return JSONResponse(content={"count": len(historical_points), "total_recorded": total, "data": historical_points})
 


@app.get("/api/logs/live")
#Fetches ONLY new data points recorded since the given index
def get_live_updates(since: int = 0):
    with buffer_lock:
        new_points = live_data_buffer[since:]
        next_id = len(live_data_buffer)
        
    return JSONResponse(content={
        "next_index": next_id, 
        "data": new_points,
        # "connection_error": connection_status["error_message"]
        "connection_status": connection_status
    })

@app.get("/")
@app.get("/index.html")
def get_dashboard():
    return FileResponse(os.path.join(script_dir, "index.html"))

# =-=-=-=-= MAIN =-=-=-=-=
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8421))
    uvicorn.run(app, host="127.0.0.1", port=port)