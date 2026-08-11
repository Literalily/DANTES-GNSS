from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import os

# Enable CORS so your local HTML file dashboard can talk to this server safely
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

script_dir = os.path.dirname(os.path.abspath(__file__))

# Mount static asset folders safely
for folder in ["css", "js", "assets"]:
    target = os.path.join(script_dir, folder)
    if os.path.exists(target):
        app.mount(f"/{folder}", StaticFiles(directory=target), name=folder)


# HTML Page Routes
@app.get("/")
@app.get("/index.html")
def get_dashboard():
    return FileResponse(os.path.join(script_dir, "index.html"))


# TODO: add GNSS API endpoints here, e.g. endpoint or websocket that 
# streams parsed NAV-PVT / MON-RF data to the dashboard.


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)