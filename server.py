"""NASA Space Survivor dashboard backend.
Run: pip install -r requirements.txt && uvicorn server:app --reload   ->  http://localhost:8000
Optional: set NASA_API_KEY (free at api.nasa.gov); DEMO_KEY works but is rate-limited.
The game can report progress with  POST /api/task/{task_id}  and the dashboard picks it up.
"""
import math, os, time
from datetime import date, timedelta
import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Space Survivor API")
KEY = os.getenv("NASA_API_KEY", "DEMO_KEY")
SWPC = "https://services.swpc.noaa.gov"
DONKI = "https://api.nasa.gov/DONKI"

LEVELS = [
    {"id": 1, "name": "Moonfall", "story": "Solar storm hits the lunar base", "tasks": [
        ("m1_escape", "Escape the damaged area"), ("m1_battery", "Collect batteries and repair power"),
        ("m1_radiation", "Avoid dangerous radiation"), ("m1_crew", "Rescue the crew, restore life support"),
        ("m1_nav", "Recover the lost navigation")],
     "data": ["Lunar terrain and elevation (LRO)", "Surface temperature", "Possible H2O ice regions", "Radiation risk (live, below)"]},
    {"id": 2, "name": "Mars Survival", "story": "A dust storm threatens the base", "tasks": [
        ("m2_resources", "Find food, H2O and resources"), ("m2_oxygen", "Repair the oxygen systems"),
        ("m2_storm", "Protect the base from the storm"), ("m2_energy", "Manage limited energy and resources")],
     "data": ["Mars rover images", "Weather: temperature and pressure", "Subsurface ice", "Dust and atmosphere"]},
    {"id": 3, "name": "Lost in Space", "story": "Find a safe destination", "tasks": [
        ("m3_navigate", "Navigate the vehicle"), ("m3_parts", "Collect spare parts"),
        ("m3_data", "Determine the correct data using mission data")],
     "data": ["Solar flares and CMEs (NASA DONKI)", "Solar wind and magnetic field (live, below)", "Energetic proton flux", "Station environment"]},
]
STATE = {"done": set()}
_cache = {}


async def get(url, params=None):
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(url, params=params)
        r.raise_for_status()
        return r.json()


def last_valid(rows, idx):
    for row in reversed(rows[1:]):
        try:
            return float(row[idx])
        except (TypeError, ValueError):
            continue


def clip(x, lo=0, hi=1):
    return max(lo, min(hi, x))


async def live_weather():
    out, live = {}, {}
    try:  # solar wind plasma + magnetic field (DSCOVR/ACE via NOAA SWPC)
        plasma = await get(f"{SWPC}/products/solar-wind/plasma-1-day.json")
        mag = await get(f"{SWPC}/products/solar-wind/mag-1-day.json")
        out.update(speed=last_valid(plasma, 2), density=last_valid(plasma, 1), bz=last_valid(mag, 3), bt=last_valid(mag, 6))
        out["series"] = [[r[0][11:16], float(r[2])] for r in plasma[1::20] if r[2]]
        live["solar_wind"] = True
    except Exception:
        live["solar_wind"] = False
    try:  # energetic protons >=10 MeV
        p = await get(f"{SWPC}/json/goes/primary/integral-protons-1-day.json")
        out["proton"] = [x for x in p if x.get("energy", "").startswith(">=10 ")][-1]["flux"]
        live["protons"] = True
    except Exception:
        live["protons"] = False
    end, start = date.today(), date.today() - timedelta(days=7)
    q = {"startDate": start.isoformat(), "endDate": end.isoformat(), "api_key": KEY}
    try:  # NASA DONKI flares + CMEs
        out["flares"] = [{"class": f["classType"], "time": f["peakTime"]} for f in (await get(f"{DONKI}/FLR", q) or [])][-5:]
        cmes = await get(f"{DONKI}/CME", q) or []
        speeds = [a["speed"] for c in cmes for a in c.get("cmeAnalyses") or [] if a.get("speed")]
        out["cme_speed"] = speeds[-1] if speeds else None
        live["donki"] = True
    except Exception:
        live["donki"] = False
    # fallback values keep the dashboard usable offline (flagged in "live")
    for k, v in dict(speed=430.0, density=5.0, bz=-2.0, bt=6.0, proton=0.4, flares=[], cme_speed=None, series=[]).items():
        out.setdefault(k, v)
    return out, live


def hazard(w):
    """0-100 hazard score from real measurements (simple, explainable weights)."""
    top = max(({"X": 20, "M": 12, "C": 4}.get(f["class"][0], 0) for f in w["flares"]), default=0)
    parts = {"solar wind": clip((w["speed"] - 400) / 500) * 25, "magnetic Bz": clip(-w["bz"] / 15) * 25,
             "protons": clip(math.log10(max(w["proton"], 0.1)) / 3) * 30, "flares": top}
    score = round(sum(parts.values()))
    level = "Calm" if score < 25 else "Elevated" if score < 50 else "Warning" if score < 75 else "Emergency"
    eta = round(1.496e8 / w["cme_speed"] / 3600, 1) if w.get("cme_speed") else None  # hours Sun->Earth
    return {"score": score, "level": level, "parts": {k: round(v) for k, v in parts.items()}, "cme_eta_hours": eta}


@app.get("/api/space-weather")
async def space_weather():
    if "w" not in _cache or time.time() - _cache["w"][0] > 60:
        w, live = await live_weather()
        _cache["w"] = (time.time(), {**w, "live": live, "hazard": hazard(w), "updated": time.strftime("%H:%M:%S")})
    return _cache["w"][1]


@app.get("/api/levels")
def levels():
    return LEVELS


@app.get("/api/state")
def state():
    return {"done": sorted(STATE["done"])}


@app.post("/api/task/{task_id}")  # call this from the game when the player finishes a task
def complete(task_id: str):
    STATE["done"].add(task_id)
    return state()


@app.post("/api/reset")
def reset():
    STATE["done"].clear()
    return state()


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index():
    return FileResponse("static/index.html")
