# =-=-=-=-= IMPORTS =-=-=-=-=
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from collections import deque
import math
import os
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

script_dir = os.path.dirname(os.path.abspath(__file__))

LOGS_DIR = os.path.join(script_dir, "logs")  # LOGS_DIR is where the historical data is held
os.makedirs(LOGS_DIR, exist_ok=True)

# Mount static asset folders safely
for folder in ["style", "js", "assets"]:
    target = os.path.join(script_dir, folder)
    if os.path.exists(target):
        app.mount(f"/{folder}", StaticFiles(directory=target), name=folder)

# =-=-=-=-= THREADING AND QUEUE =-=-=-=-=
solution_queue = queue.Queue()
live_data_buffer = []
buffer_lock = threading.Lock()

# =-=-=-=-= SSE (SERVER-SENT EVENTS) SUBSCRIBERS =-=-=-=-=
sse_subscribers = []
sse_subscribers_lock = threading.Lock()
main_event_loop = None 

@app.on_event("startup")
async def capture_event_loop():
    global main_event_loop
    main_event_loop = asyncio.get_running_loop()

def broadcast_to_subscribers(item):
    if main_event_loop is None:
        return
    with sse_subscribers_lock:
        subscribers = list(sse_subscribers)
    for subscriber_queue in subscribers:
        main_event_loop.call_soon_threadsafe(subscriber_queue.put_nowait, item)

# =-=-=-=-= BASELINE CONFIGURATION =-=-=-=-=
# Each baseline entry corralates to a unique RTKNAVI instance output stream.
# In format E/N/U Baseline (switched from lat/lon/alt becuase it works just as well without difficult backend calculations).
# this reads the text solution lines it prints.
# port 5001 -- 0.28m -- port 5000 -- 0.28m -- port 5002 
# 5000 (the middle antenna) is used as the common RTK base for both baselines.
BASELINES = [
    {
        "name": "5000-5001",          # base 5000, rover 5001
        "host": "127.0.0.1",
        "port": 6001,                 # RTKNAVI instance A's Solution 1 output port
        "nominal_distance_m": 0.28,
        "warning_tolerance_m": 0.03,  # if it deviates more than 3cm either way, change status badge to WARNING
        "alarm_tolerance_m": 0.08,    # if it deviates more than 8cm either way, change status badge to ALARM (spoofing suspected)
    },
    {
        "name": "5000-5002",          # base 5000, rover 5002
        "host": "127.0.0.1",
        "port": 6002,                 # RTKNAVI instance B's Solution 1 output port
        "nominal_distance_m": 0.28,
        "warning_tolerance_m": 0.03,
        "alarm_tolerance_m": 0.08,
    },
]

connection_status = {
    b["name"]: {
        "is_connected": False,
        "error_message": None,
        "last_packet_time": 0,
    }
    for b in BASELINES
}

# for checking if connection is stale and then alerting the dashboard
CONNECTION_STALE_THRESHOLD = 2.5


# =-=-=-=-= PARSE RTKLIB SOLUTION LINES =-=-=-=-=
# RTKNAVI's "E/N/U-Baseline" solution stream looks like: date time e n u Q ns sde sdn sdu (sdeu, sdun, sdue, age, ratio)
# e.g. 2026/09/02 14:03:11.00   0.264   -0.085   0.0012   1   9 (only the first 7 are important)

def parse_rtklib_solution_line(line, baseline_name):
    line = line.strip()
    if not line or line.startswith("%"):
        return None  #header line

    parts = line.split()
    if len(parts) < 7:
        return None  # if it's an incomplete line, eg. a partial read, skip it

    try:
        date_str, time_str = parts[0], parts[1]
        e = float(parts[2])
        n = float(parts[3])
        u = float(parts[4])
        quality = int(parts[5])
        num_sats = int(parts[6])
    except ValueError:
        return None  # if it's a corrupted/partial line, skip don't crash the reader thread over this

    # reformat datetime to DD/MM/YYYY
    try:
        year, month, day = date_str.split("/")
        date_str = f"{day}/{month}/{year}"
    except ValueError:
        pass

    distance_m = math.sqrt(e * e + n * n + u * u)

    return {
        "time": f"{date_str} {time_str}",
        "baseline": baseline_name,
        "e": round(e, 4),
        "n": round(n, 4),
        "u": round(u, 4),
        "distance_m": round(distance_m, 4),
        "q": quality,     # 1=fix, 2=float, 3=sbas, 4=dgps, 5=single, 6=ppp, 0=no solution
        "ns": num_sats,
    }


def classify_status(baseline_cfg, parsed_point):
    # compare calulated distance between ports to expected physical distance
    if parsed_point["q"] not in (1, 2):
        return "warning"  # no usable fix yet (not necessarily a spoof, but not trustworthy either)

    deviation = abs(parsed_point["distance_m"] - baseline_cfg["nominal_distance_m"])
    if deviation > baseline_cfg["alarm_tolerance_m"]:
        return "alarm" #if deviation significantly larger than expected, set status to spoofing alarm
    if deviation > baseline_cfg["warning_tolerance_m"]:
        return "warning" #if deviation slightly larger than expected, set status to spoofing alarm
    return "normal" #if all okay, normal


# =-=-=-=-= THREAD: RTKNAVI SOLUTION STREAM READER (one per baseline) =-=-=-=-=
def baseline_socket_thread(baseline_cfg):
    name = baseline_cfg["name"]
    host, port = baseline_cfg["host"], baseline_cfg["port"]

    print(f"[{name}] Socket worker started. Connecting to RTKNAVI solution port {host}:{port} ...")

    while True:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5.0)
            sock.connect((host, port))

            connection_status[name]["is_connected"] = True
            connection_status[name]["error_message"] = None
            print(f"[{name}] Connected to RTKNAVI solution stream.")

            stream = sock.makefile("r")  # text mode since RTKLIB solution output is ASCII
            lines_in_session = 0
            session_start = time.time()

            for line in stream:
                parsed = parse_rtklib_solution_line(line, name)
                if parsed:
                    lines_in_session += 1
                    connection_status[name]["last_packet_time"] = time.time()
                    connection_status[name]["error_message"] = None
                    parsed["status"] = classify_status(baseline_cfg, parsed)
                    parsed["nominal_distance_m"] = baseline_cfg["nominal_distance_m"]
                    solution_queue.put(parsed)

                if lines_in_session == 0 and (time.time() - session_start) > 5.0:
                    raise TimeoutError("Connected to RTKNAVI but no solution lines received in 5s.")

        except (socket.timeout, TimeoutError, ConnectionRefusedError, OSError) as e:
            err_msg = (
                f"[CRITICAL ERROR] Could not reach RTKNAVI solution stream for baseline {name} "
                f"({host}:{port}). Please check your setup by clicking 'Setup Guide'. "
                f"Likely to be: 1) RTKNAVI instance not running, or "
                f"2) Solution 1 output not set to 'TCP Server' on this port."
            )
            print(f"\n{'='*80}\n{err_msg}\n{'='*80}\n")
            connection_status[name]["is_connected"] = False
            connection_status[name]["error_message"] = err_msg

        except Exception as e:
            print(f"[{name} WARNING] Connection error: {e}")

        finally:
            try:
                sock.close()
            except Exception:
                pass

        time.sleep(5)  # retry pause


# =-=-=-=-= THREAD: DATA PROCESSOR & LOGGING =-=-=-=-=
# 1. pulls parsed solutions off the queue
# 2. appends them to a single master CSV log (one row per epoch per baseline)
# 3. keeps a live buffer and pushes to connected dashboard over SSE
def data_processor_thread():
    print("[LOGGER] Consumer processor started.")
    master_log_filepath = os.path.join(LOGS_DIR, "baseline_master_log.csv")

    while True:
        try:
            item = solution_queue.get(timeout=1.0)

            file_exists = os.path.exists(master_log_filepath)
            with open(master_log_filepath, "a") as f:
                if not file_exists:
                    f.write("datetime,baseline,e,n,u,distance_m,nominal_distance_m,q,ns,status\n")
                f.write(
                    f"{item['time']},{item['baseline']},{item['e']},{item['n']},{item['u']},"
                    f"{item['distance_m']},{item['nominal_distance_m']},{item['q']},{item['ns']},{item['status']}\n"
                )

            with buffer_lock:
                item["id"] = len(live_data_buffer)
                live_data_buffer.append(item)

            broadcast_to_subscribers(item) #put onto dashboard

            solution_queue.task_done()
            print(
                f"[SUCCESS] {item['time']} | {item['baseline']} | "
                f"dist={item['distance_m']}m (nominal {item['nominal_distance_m']}m) | "
                f"Q={item['q']} ns={item['ns']} | {item['status'].upper()}"
            )

        except queue.Empty:
            continue
        except Exception as e:
            print(f"[ERROR] Failed to save or process solution: {e}")


# Start one background reader thread per configured baseline, plus the logger
for baseline_cfg in BASELINES:
    threading.Thread(target=baseline_socket_thread, args=(baseline_cfg,), daemon=True).start()
threading.Thread(target=data_processor_thread, daemon=True).start()


# =-=-=-=-= APIs =-=-=-=-=
@app.get("/api/logs/history")
def get_historical_logs(max_points: int = 3000):
    # Scans ./logs folder and loads all historical points on launch (skips malformed lines if server crashes mid-write)
    historical_points = []
    log_files = sorted([f for f in os.listdir(LOGS_DIR) if f.endswith(".csv")])
    for log_file in log_files:
        filepath = os.path.join(LOGS_DIR, log_file)
        with open(filepath, "r") as f:
            lines = f.readlines()[1:]
            for line in lines:
                parts = line.strip().split(",")
                if len(parts) != 10:
                    continue
                try:
                    historical_points.append({
                        "time": parts[0],
                        "baseline": parts[1],
                        "e": float(parts[2]),
                        "n": float(parts[3]),
                        "u": float(parts[4]),
                        "distance_m": float(parts[5]),
                        "nominal_distance_m": float(parts[6]),
                        "q": int(parts[7]),
                        "ns": int(parts[8]),
                        "status": parts[9],
                    })
                except ValueError:
                    continue  # malformed row - skip rather than 500

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
    points = []
    log_files = sorted([f for f in os.listdir(LOGS_DIR) if f.endswith(".csv")])
    total_recorded = 0
    for log_file in log_files:
        filepath = os.path.join(LOGS_DIR, log_file)
        with open(filepath, "r") as f:
            next(f, None)  # skip header
            tail_lines = deque(f, maxlen=max_points)
        for line in tail_lines:
            total_recorded += 1
            parts = line.strip().split(",")
            if len(parts) != 10:
                continue
            try:
                points.append({
                    "baseline": parts[1],
                    "e": float(parts[2]),
                    "n": float(parts[3]),
                    "u": float(parts[4]),
                    "distance_m": float(parts[5]),
                    "nominal_distance_m": float(parts[6]),
                    "q": int(parts[7]),
                })
            except ValueError:
                continue

    capped = total_recorded >= max_points
    if len(points) > max_points:
        points = points[-max_points:]

    by_baseline = {}
    for pt in points:
        by_baseline.setdefault(pt["baseline"], []).append(pt)

    baseline_stats = {}
    for name, pts in by_baseline.items():
        distances = [p["distance_m"] for p in pts]
        es = [p["e"] for p in pts]
        ns = [p["n"] for p in pts]
        us = [p["u"] for p in pts]
        fixed_count = sum(1 for p in pts if p["q"] == 1)
        baseline_stats[name] = {
            "count": len(pts),
            "nominal_distance_m": pts[-1]["nominal_distance_m"],
            "avg_distance_m": sum(distances) / len(distances),
            "max_distance_m": max(distances),
            "min_distance_m": min(distances),
            "avg_e": sum(es) / len(es),
            "avg_n": sum(ns) / len(ns),
            "avg_u": sum(us) / len(us),
            "fix_rate_pct": round(100 * fixed_count / len(pts), 1),
        }

    return JSONResponse(content={
        "total_recorded": total_recorded,
        "points_considered": len(points),
        "capped": capped,
        "baselines": baseline_stats,
    })
    
@app.get("/api/connection/status")
def get_connection_status():
    # reports baseline connection health + alerts the dashboard if connection is stale
    now = time.time()
    result = {}
    for name, status in connection_status.items():
        last_packet_time = status["last_packet_time"]
        
        if last_packet_time == 0:
            #never received a valid solution line on the connecion attempt
            seconds_since_last_packet = None
            is_stale = not status["is_connected"]
        else:
            seconds_since_last_packet = round(now - last_packet_time, 1)
            is_stale = (not status["is_connected"]) or (seconds_since_last_packet > CONNECTION_STALE_THRESHOLD)
            
        result[name] = {
            "is_connected": status["is_connected"],
            "error_message": status["error_message"],
            "seconds_since_last_packet": seconds_since_last_packet,
            "is_stale": is_stale,
        }
    return JSONResponse(content=result)

@app.get("/api/logs/stream")
async def stream_logs(request: Request):
    client_queue = asyncio.Queue()
    with sse_subscribers_lock:
        sse_subscribers.append(client_queue)

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(client_queue.get(), timeout=5.0)
                    yield f"data: {json.dumps(item)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            with sse_subscribers_lock:
                if client_queue in sse_subscribers:
                    sse_subscribers.remove(client_queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")

# for the 'stop server' button
@app.post("/api/shutdown")
def shutdown_server():
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
