# DANTES-GNSS
## GNSS Time Signal Spoofing Detector

> **School of Electronics, Electrical Engineering and Computer Science (EEECS)**  
> **Summer Research Internship 2026** | *Queen's University Belfast*  
> **Supervisors:** Dr. David Laverty & Dr. Iman Okasili  

---

## 📌 About the Project

DANTES-GNSS (**D**istributed **A**ntenna **N**etwork for **T**iming & **E**rror **S**urveillance) is a real-time dashboard for detecting GNSS spoofing attacks against the time signals relied on by electrical substation equipment such as Phasor Measurement Units (PMUs) and Merging Units (MUs).  

Rather than requiring specialist antennas, cryptographic authentication, or custom baseband hardware, the system exploits a simple geometric property: three closely-spaced GNSS receive antennas should always report positions with a very clear, well-known separation from one another. A terrestrial spoofing signal aimed at one antenna cannot be uniquely tailored to a second antenna a short distance away, so under attack the reported positions of the affected receivers collapse toward a single, identical location — an outcome that is physically impossible under genuine satellite reception.  

The system uses **RTKLIB** (`RTKNAVI` / `STRSVR`) to compute the real-time kinematic (RTK) baseline vector between a shared central "base" receiver (port `5000`) and two outer "rover" receivers (ports `5001` and `5002`). A Python/FastAPI backend continuously ingests both baseline solution streams, logs them, classifies their deviation from the expected nominal baseline distance, and pushes live updates to a browser dashboard, which visualises the vectors, flags drift, and raises an alarm when spoofing is suspected or confirmed.  

---

## ✨ Key Features  

* **Dual-Baseline RTK Monitoring:** Reads live E/N/U baseline solutions from two independent RTKNAVI instances sharing a common base receiver, over TCP.  
* **Live Vector Dashboard:** Interactive polar-style scatter chart (Chart.js) plotting each baseline's real-time East/North deviation around the fixed base station, with a fading trail showing recent history.  
* **Tiered Status Alerts:** Automatic classification of each baseline as `NORMAL`, `WARNING`, or `ALARM: SPOOFING SUSPECTED` based on configurable deviation tolerances and fix quality.  
* **Position-Coincidence Spoofing Check:** Independently flags a hard `SPOOFING DETECTED` state if two baselines ever report the same position to within centimetre tolerance — a scenario with no innocent explanation.  
* **Connection Health Monitoring:** Detects and reports when a RTKNAVI/STRSVR stream stalls or disconnects, distinguishing a lost link from a genuine spoofing event.  
* **Historical Logging & Statistics:** All solutions are appended to CSV logs and summarised into running distance statistics (average/min/max distance, fix rate, sample count) per baseline.  
* **Live Streaming via SSE:** New solution points are pushed to the browser over Server-Sent Events, with a one-off history fetch on page load so the dashboard is never empty.  

---

## 🛠️ Technology Stack  

| Domain | Technologies |  
| :--- | :--- |
| **GNSS Positioning Engine** | RTKLIB (`RTKNAVI`, `STRSVR`) |
| **Backend & Web Server** | Python 3, FastAPI, Uvicorn |
| **Data Ingestion** | Raw TCP sockets (RTKLIB "E/N/U-Baseline" solution stream), threaded per-baseline readers |
| **Frontend Dashboard** | HTML5, CSS3, JavaScript (ES6+), Chart.js |
| **Data Format** | CSV solution logs, JSON over REST + Server-Sent Events (SSE) |
| **Environment Management** | Python `venv`, `pip` (via `app.py` / `launch.bat`) |

---

## 🎯 Required Installations  

### === **RTKLIB** ===  
1) Download RTKLIB from [rtkexplorer.com](https://rtkexplorer.com/downloads/rtklib-code/) (or the [GitHub repo](https://github.com/tomojitakasu/RTKLIB)).  
2) The system requires `RTKNAVI.exe` and `STRSVR.exe` to communicate with the GNSS receivers — no other RTKLIB tools are needed.  

### === **Physical Layout** ===  
This system is designed around three sky-facing antennas placed in a straight line, roughly 0.28 m apart, connected to receivers reachable on ports `5001` – `5000` – `5002` (left to right). The **middle receiver (`5000`)** acts as the common RTK base for both baselines.  

### === **Python** ===  
1) A Python 3 installation is required to run `app.py` and `server.py`.  
2) No manual `pip install` is needed — the environment and dependencies (`fastapi`, `uvicorn`, `pydantic`, `pyserial`, `pyubx2`, see `requirements.txt`) are installed automatically by the setup script described below.  

---

## 📌 Setting Up and Using the System  

1) **Launch `STRSVR`** first, since port `5000` can only accept one client connection at a time:  
   - Input: TCP Client → the receiver's address (e.g. `143.117.216.46`)  
   - Output: TCP Server → a free local port (e.g. `6000`)  
   - Point both RTKNAVI base inputs at this relay port instead of `5000` directly.  

2) **Launch two `RTKNAVI` instances**, one per baseline:  

   | Setting | Instance A | Instance B |
   | :--- | :--- | :--- |
   | Rover input (Stream 1) | TCP Client → port `5001` | TCP Client → port `5002` |
   | Base input (Stream 2) | TCP Client → port `6000` (relay) | TCP Client → port `6000` (relay) |
   | Solution 1 output | TCP Server, port `6001` | TCP Server, port `6002` |
   | Solution format | E/N/U-Baseline | E/N/U-Baseline |
   | Positioning mode | Kinematic | Kinematic |
   | Ambiguity resolution | Fix and Hold | Fix and Hold |

> [!NOTE]
> The Solution 1 output ports (`6001` / `6002`) must match the `BASELINES` list at the top of `server.py` — update one side or the other if different ports are used.  

3) **Run the first-time setup.** Open a terminal in the project directory and run:  
   ```
   py app.py  
   ```
   This checks for the local virtual environment (`venvGNSSProject`), and if missing, creates it and installs everything listed in `requirements.txt`.  

4) **Start the dashboard.** Double-click `launch.bat`. It re-checks dependencies, confirms port `8421` is free, starts `server.py`, and automatically opens the dashboard in your browser at `http://127.0.0.1:8421`.  

5) **Confirm normal operation.** Once RTKNAVI achieves a fixed solution (Q=1) on both instances, the status badge should read **NORMAL**, and both baseline distances on the dashboard should settle close to the configured nominal separation (**0.28 m** by default).  

6) **To close the program**, use the **Stop Server** button on the dashboard, or return to the terminal window running `server.py` and press `CTRL+C`.  

---

## 📌 Understanding System Statuses  

| Status | Meaning |
| :--- | :--- |
| 🟢 **NORMAL** | Both baselines are within their configured tolerance of the expected nominal distance. |
| 🟠 **WARNING** | A baseline has drifted beyond its warning tolerance, or has lost a usable fix (float/no solution) — not necessarily a spoof, but not yet trustworthy. |
| 🔴 **ALARM: SPOOFING SUSPECTED** | A baseline's distance has deviated beyond the alarm tolerance while still reporting a fix — the signature behaviour of a spoofing attack. |
| ⚫ **SPOOFING DETECTED** | Two or more baselines are independently reporting an identical position to within centimetre precision. This has no innocent explanation and is treated as definitive confirmation of an attack. |
| 🟥 **ERROR** | The dashboard has lost its connection to one of the RTKNAVI/STRSVR streams, or to the backend server itself — this is a connectivity fault, not a spoofing event, and should be resolved by checking that RTKNAVI, STRSVR, and `server.py` are still running. |

Historical WARNING and ALARM events are recorded under **Spoofing History** on the dashboard, showing the most recent deviation, the affected baseline, and the time it occurred, so that transient events are not missed between visits to the dashboard.  
