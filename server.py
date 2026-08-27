# =-=-=-=-= IMPORTS =-=-=-=-=
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pyubx2 import UBXReader, UBX_PROTOCOL, NMEA_PROTOCOL
from collections import deque
import math
import os
import sys
import time
import datetime
import socket
import threading
import queue
import asyncio
import json
import signal
import subprocess

# Enable CORS so local HTML file dashboard can talk to server safely
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# convert lat/lon into meters (also assumes 54o in case I want to make it dynamic later - TODO lily)
METRES_PER_DEG_LAT = 111120
METRES_PER_DEG_LON = 65315

script_dir = os.path.dirname(os.path.abspath(__file__))

print(f"[DEBUG] script_dir = {script_dir}")
print(f"[DEBUG] style folder exists: {os.path.exists(os.path.join(script_dir, 'style'))}")

LOGS_DIR = os.path.join(script_dir, "logs") #LOGS_DIR is where the historical data is held
os.makedirs(LOGS_DIR, exist_ok=True)

# Mount static asset folders safely
for folder in ["style", "js", "assets"]:
    target = os.path.join(script_dir, folder)
    if os.path.exists(target):
        app.mount(f"/{folder}", StaticFiles(directory=target), name=folder)

# =-=-=-=-= THREADING AND QUEUE =-=-=-=-=
gnss_queue = queue.Queue()
live_data_buffer = []
buffer_lock = threading.Lock()

# =-=-=-=-= SSE (SERVER-SENT EVENTS) SUBSCRIBERS =-=-=-=-=
# Instead of the browser polling every second, each connected browser tab gets
# its own asyncio.Queue here. The background worker thread pushes new points
# into every subscriber's queue as soon as they're parsed, and the /api/logs/stream
# endpoint streams them straight out over one long-lived HTTP connection.
sse_subscribers = []
sse_subscribers_lock = threading.Lock()
main_event_loop = None  # set on FastAPI startup; needed to safely hand data from the worker thread to asyncio


@app.on_event("startup")
async def capture_event_loop():
    global main_event_loop
    main_event_loop = asyncio.get_running_loop()


def broadcast_to_subscribers(item):
    # Called from the background worker thread (not async), so we use
    # call_soon_threadsafe to safely wake up each subscriber's asyncio queue.
    if main_event_loop is None:
        return
    with sse_subscribers_lock:
        subscribers = list(sse_subscribers)
    for subscriber_queue in subscribers:
        main_event_loop.call_soon_threadsafe(subscriber_queue.put_nowait, item)

# GNSS receiver configuration to track multiple receivers at once
# every point recorded is tagged with the "host:port" it came from so the dashboard can tell the receivers apart
RECEIVERS = [
    {"host": "143.117.216.46", "port": 5002},
    {"host": "143.117.216.46", "port": 5001},
    {"host": "143.117.216.46", "port": 5000}
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
                
            # Push straight to browser instead of waiting for it to poll
            broadcast_to_subscribers(item)

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


# Start one background receiver thread per configured receiver, plus the logger
for r in RECEIVERS:
    threading.Thread(target=gnss_socket_thread, args=(r["host"], r["port"]), daemon=True).start()
threading.Thread(target=data_processor_thread, daemon=True).start()


# =-=-=-=-= APIs =-=-=-=-=
@app.get("/api/logs/history")
def get_historical_logs(max_points: int = 3000):
    # Scans ./logs folder and loads all historical points on launch.
    # If there are more rows than max_points, the data is evenly thinned
    # out (always keeping the most recent point) so the chart stays fast
    # and readable instead of trying to plot every single raw sample
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
 

@app.get("/api/logs/distance_stats")
def get_distance_stats(max_points: int = 20000):
    # v similar to /api/logs/history but for the table instead of the chart
    # has a larger cap and reads from tail
    points = []
    log_files = sorted([f for f in os.listdir(LOGS_DIR) if f.endswith('.csv')])
    total_recorded = 0
    for log_file in log_files:
        filepath = os.path.join(LOGS_DIR, log_file)
        with open(filepath, 'r') as f:
            next(f, None)  # skip header
            tail_lines = deque(f, maxlen=max_points)
        for line in tail_lines:
            total_recorded += 1
            parts = line.strip().split(',')
            if len(parts) == 5:
                try:
                    points.append({
                        "latitude": float(parts[1]),
                        "longitude": float(parts[2]),
                        "receiver": parts[4]
                    })
                except ValueError:
                    continue
 
    capped = total_recorded >= max_points
    if len(points) > max_points:
        points = points[-max_points:]
 
    # Group points per receiver, in chronological order
    by_receiver = {}
    for pt in points:
        by_receiver.setdefault(pt["receiver"], []).append(pt)
 
    # --- Per-receiver lat/lon stats (mean / max / min) ---
    receiver_stats = {}
    for receiver_id, pts in by_receiver.items():
        lats = [p["latitude"] for p in pts]
        lons = [p["longitude"] for p in pts]
        receiver_stats[receiver_id] = {
            "port": receiver_id.split(":")[-1],
            "count": len(pts),
            "avg_lat": sum(lats) / len(lats),
            "avg_lon": sum(lons) / len(lons),
            "max_lat": max(lats),
            "max_lon": max(lons),
            "min_lat": min(lats),
            "min_lon": min(lons),
        }
 
    # receivers log independently, so pairing is done by matching
    # each receiver's Nth-most-recent reading to the other's Nth-most-recent
    # reading (not by exact timestamp). Good enough to characterise how far
    # apart reported positions drift, but not a simultaneous fix-to-fix
    # distance if receivers are logging at different rates.
    receiver_ids = list(by_receiver.keys())
    pair_stats = {}
    for i in range(len(receiver_ids)):
        for j in range(i + 1, len(receiver_ids)):
            id_a, id_b = receiver_ids[i], receiver_ids[j]
            pts_a, pts_b = by_receiver[id_a][::-1], by_receiver[id_b][::-1]  # align from most-recent backwards
            n = min(len(pts_a), len(pts_b))
            if n == 0:
                continue
 
            lat_diffs, lon_diffs, distances = [], [], []
            for k in range(n):
                lat_diff = pts_a[k]["latitude"] - pts_b[k]["latitude"]
                lon_diff = pts_a[k]["longitude"] - pts_b[k]["longitude"]
                dy = lat_diff * METRES_PER_DEG_LAT
                dx = lon_diff * METRES_PER_DEG_LON
                distances.append(math.sqrt(dx * dx + dy * dy))
                lat_diffs.append(lat_diff)
                lon_diffs.append(lon_diff)
 
            max_idx = distances.index(max(distances))
            min_idx = distances.index(min(distances))
 
            pair_key = f"{id_a.split(':')[-1]}-{id_b.split(':')[-1]}"
            pair_stats[pair_key] = {
                "n_points_compared": n,
                "average": {
                    "lat_diff": sum(lat_diffs) / n,
                    "lon_diff": sum(lon_diffs) / n,
                    "distance_m": sum(distances) / n,
                },
                "max_distance": {
                    "lat_diff": lat_diffs[max_idx],
                    "lon_diff": lon_diffs[max_idx],
                    "distance_m": distances[max_idx],
                },
                "min_distance": {
                    "lat_diff": lat_diffs[min_idx],
                    "lon_diff": lon_diffs[min_idx],
                    "distance_m": distances[min_idx],
                },
            }
 
    return JSONResponse(content={
        "total_recorded": total_recorded,
        "points_considered": len(points),
        "capped": capped,
        "receivers": receiver_stats,
        "pairs": pair_stats
    })

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
    
@app.get("/api/logs/stream")
async def stream_logs(request: Request):
    # Each browser tab gets its own queue registered above; the worker thread pushes into it via broadcast_to_subscribers() whenever a new point is parsed.
    client_queue = asyncio.Queue()
    with sse_subscribers_lock:
        sse_subscribers.append(client_queue)

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(client_queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(item)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            with sse_subscribers_lock:
                if client_queue in sse_subscribers:
                    sse_subscribers.remove(client_queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")

# for the 'restart server' button
def run_launch_script():
    # Terminate current server instance
    os.kill(os.getpid(), signal.SIGINT)
    # runs launch.bat in a separate process
    subprocess.Popen(["launch.bat"], shell=True, creationflags=subprocess.CREATE_NEW_CONSOLE)


@app.post("/api/restart")
def restart_server(background_tasks: BackgroundTasks):
    # Schedules execution immediately after the response is sent
    background_tasks.add_task(run_launch_script)
    return {"message": "Server restarting..."}

# for the 'stop server' button
@app.post("/api/shutdown")
def shutdown_server():
    # Sends a SIGINT signal to the process, stopping uvicorn and closing the cmd window
    os.kill(os.getpid(), signal.SIGINT)
    return {"message": "Server shutting down..."}

@app.get("/")
@app.get("/index.html")
def get_dashboard():
    return FileResponse(os.path.join(script_dir, "index.html"))


# =-=-=-=-= MAIN =-=-=-=-=
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8421))
    uvicorn.run(app, host="127.0.0.1", port=port)