import math
import random
import time
import secrets
import json
import os
import asyncio
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Dict, Optional, List

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import FileResponse
from pydantic import BaseModel
import httpx
import uvicorn

# ---------- Database ----------
import databases
import sqlalchemy
from sqlalchemy import create_engine, MetaData, Table, Column, Integer, String, Float, DateTime, JSON, Boolean
from sqlalchemy.sql import select

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./omni_data.db")
database = databases.Database(DATABASE_URL)
metadata = MetaData()

campaigns_table = Table(
    "campaigns",
    metadata,
    Column("id", String, primary_key=True),
    Column("api_url", String),
    Column("api_key", String),
    Column("service_id", Integer),
    Column("link", String),
    Column("target", Integer),
    Column("min_views", Integer),
    Column("max_views", Integer),
    Column("vibe", String),
    Column("timezone_offset", Integer),
    Column("drip_interval_minutes", Integer),
    Column("current", Integer),
    Column("last_views", Integer),
    Column("next_drip", Float),
    Column("status", String),
    Column("history", JSON),
    Column("last_order_id", Integer, nullable=True),
    Column("last_order_status", String, nullable=True),
    Column("drip_count", Integer),
    Column("created", DateTime),
    Column("safe_mode", Boolean),
    Column("safety_factor", Float),
    Column("smart_boost", Boolean),
    Column("project", String),
    Column("velocity_target_hours", Float),
    Column("last_order_duration", Float, nullable=True),
    Column("recent_drips", JSON),
    Column("stealth_phase", Float),
    Column("social_boost_scheduled", Float, nullable=True),
    Column("social_boost_views", Integer, nullable=True),
    Column("_order_check_time", Float, nullable=True),
)

engine = create_engine(DATABASE_URL)
metadata.create_all(engine)

# ---------- FastAPI app ----------
app = FastAPI(title="OMNI-DRIP ULTRA")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Authentication ----------
VALID_USERNAME = "admin"
VALID_PASSWORD = "sharedpass"
active_tokens = {}
security = HTTPBearer()

class LoginRequest(BaseModel):
    username: str
    password: str

@app.post("/login")
async def login(creds: LoginRequest):
    if creds.username == VALID_USERNAME and creds.password == VALID_PASSWORD:
        token = secrets.token_urlsafe(32)
        active_tokens[token] = creds.username
        return {"token": token, "username": creds.username}
    raise HTTPException(status_code=401, detail="Invalid credentials")

@app.post("/logout")
async def logout(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    if token in active_tokens:
        del active_tokens[token]
    return {"status": "logged out"}

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    if token not in active_tokens:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return token

# ---------- WebSocket Manager ----------
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except:
                pass

manager = ConnectionManager()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = Query(...)):
    if token not in active_tokens:
        await websocket.close(code=1008)
        return
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# ---------- Models ----------
class APIConfig(BaseModel):
    api_url: str
    api_key: str

class CampaignConfig(BaseModel):
    api_url: str
    api_key: str
    service_id: int
    link: str
    target: int
    min_views: int = 100
    max_views: int = 300
    vibe: str = "hybrid"
    timezone_offset: int = 0
    drip_interval_minutes: int = 30
    safe_mode: bool = False
    safety_factor: float = 0.7
    smart_boost: bool = False
    project: str = "default"
    velocity_target_hours: float = 24.0
    speed_multiplier: float = 1.0
    finish_hours: Optional[float] = None

# ---------- Global in-memory stores (tasks, locks, api config) ----------
active_tasks: Dict[str, asyncio.Task] = {}
drip_locks: Dict[str, asyncio.Lock] = {}
api_verified = False
api_config = {"url": "", "key": "", "balance": "0"}

# ---------- Drip Engine (copied from your original) ----------
class AdaptiveDripScheduler:
    def __init__(self, camp: dict):
        self.camp = camp
        self.camp.setdefault("vibe", "hybrid")
        self.camp.setdefault("timezone_offset", 0)
        self.camp.setdefault("velocity_target_hours", 24.0)
        self.camp.setdefault("last_order_duration", None)
        self.camp.setdefault("recent_drips", [])
        self.camp.setdefault("stealth_phase", random.uniform(0, 4))
        self.camp.setdefault("social_boost_scheduled", None)
        self.camp.setdefault("social_boost_views", None)

    @staticmethod
    def activity_multiplier(utc_dt: datetime, tz_offset: int, stealth_phase: float) -> float:
        local = utc_dt + timedelta(hours=tz_offset + stealth_phase)
        hour = local.hour + local.minute / 60.0
        peak1 = math.exp(-((hour - 11.5) ** 2) / (2 * 3.0 ** 2))
        peak2 = math.exp(-((hour - 20.5) ** 2) / (2 * 3.0 ** 2))
        activity = 0.3 + 1.2 * max(peak1, peak2)
        wd = local.weekday()
        if wd == 4:   activity *= 1.10
        elif wd == 5: activity *= 1.15
        elif wd == 6: activity *= 0.80
        return max(0.2, min(1.5, activity))

    def _organic_view_curve(self, progress: float) -> float:
        center, sigma = 0.6, 0.2
        bell = math.exp(-((progress - center) ** 2) / (2 * sigma ** 2))
        noise = random.gauss(0, 0.12)
        return max(0.05, min(1.0, bell + noise))

    def _should_social_boost(self) -> bool:
        if not self.camp.get("smart_boost", False):
            return False
        remaining = self.camp["target"] - self.camp["current"]
        if remaining < 300:
            return False
        return random.random() < random.uniform(0.02, 0.08)

    def _compute_social_boost_views(self, last_views: int) -> int:
        return int(last_views * random.uniform(0.3, 0.7))

    def determine_views(self) -> int:
        camp = self.camp
        target = camp["target"]
        current = camp.get("current", 0)
        remaining = target - current
        if remaining <= 0:
            return 0

        if camp["social_boost_scheduled"] is not None:
            if time.time() >= camp["social_boost_scheduled"]:
                views = camp["social_boost_views"]
                camp["social_boost_scheduled"] = None
                camp["social_boost_views"] = None
                if remaining >= 100:
                    views = max(100, views)
                    views = min(views, remaining)
                else:
                    views = 100
                return max(0, views)
            return 0

        progress = current / target if target > 0 else 0
        vf = self._organic_view_curve(progress)
        min_v = max(camp.get("min_views", 100), 100)
        max_v = camp.get("max_views", 600)
        views = min_v + (max_v - min_v) * vf

        now = datetime.utcnow()
        tod = self.activity_multiplier(now, camp.get("timezone_offset", 0), camp["stealth_phase"])
        views *= tod

        elapsed_hours = (time.time() - self._campaign_start_time()) / 3600.0
        target_hours = camp.get("velocity_target_hours", 24.0)
        expected = min(1.0, elapsed_hours / target_hours) if target_hours > 0 else 1.0
        if progress < expected:
            views *= 1.0 + min(0.3, (expected - progress) * 0.5)
        elif progress > expected + 0.05:
            views *= max(0.8, 1.0 - (progress - expected) * 0.5)

        if camp.get("last_order_duration") and camp["last_order_duration"] > 1800:
            views *= 0.9

        vibe = camp.get("vibe", "hybrid")
        if vibe == "viral" and 0.3 < progress < 0.8:
            views *= random.uniform(1.05, 1.15)
        elif vibe == "stealth" and random.random() < 0.3:
            views *= 0.85

        views = int(round(views))
        if remaining >= 100:
            views = max(100, views)
            views = min(views, remaining)
        else:
            views = 100

        if views > 0 and self._should_social_boost():
            boost = self._compute_social_boost_views(views)
            if boost >= 100:
                camp["social_boost_scheduled"] = time.time() + random.uniform(300, 900)
                camp["social_boost_views"] = boost

        return views

    def determine_interval(self, views_sent: int) -> float:
        camp = self.camp
        base_min = camp.get("drip_interval_minutes", 30)
        base = base_min * 60

        now = datetime.utcnow()
        tod = self.activity_multiplier(now, camp.get("timezone_offset", 0), camp["stealth_phase"])
        interval = base * (1.0 / max(tod, 0.3))

        current = camp.get("current", 0)
        target = camp["target"]
        progress = current / target if target > 0 else 0
        elapsed_hours = (time.time() - self._campaign_start_time()) / 3600.0
        target_hours = camp.get("velocity_target_hours", 24.0)
        expected = min(1.0, elapsed_hours / target_hours) if target_hours > 0 else 1.0

        if progress < expected:
            interval *= max(0.7, 1.0 - (expected - progress) * 0.8)
        elif progress > expected + 0.05:
            interval *= min(1.5, 1.0 + (progress - expected) * 0.8)

        if camp.get("last_order_duration"):
            dur = camp["last_order_duration"]
            if dur > 1800:
                interval *= 0.85
            elif dur < 120:
                interval *= 1.20

        if len(camp["recent_drips"]) >= 2:
            last = camp["recent_drips"][-1]
            prev = camp["recent_drips"][-2]
            if last - prev < 1200:
                interval *= 1.30

        vibe = camp.get("vibe", "hybrid")
        if vibe == "viral":
            interval *= random.uniform(0.9, 1.1)
        elif vibe == "stealth":
            interval *= random.uniform(1.1, 1.4)

        interval *= random.uniform(0.85, 1.15)
        interval = max(60, min(10800, interval))

        camp["recent_drips"].append(time.time())
        if len(camp["recent_drips"]) > 5:
            camp["recent_drips"] = camp["recent_drips"][-5:]

        return interval

    def _campaign_start_time(self) -> float:
        try:
            return datetime.fromisoformat(self.camp["created"]).timestamp()
        except:
            return time.time()

# ---------- wait_for_order_completion (copied from your original) ----------
async def wait_for_order_completion(api_url: str, api_key: str, order_id: int, camp: dict) -> None:
    completed_statuses = [
        "completed", "success", "finished", "done", "complete",
        "partial", "canceled", "error", "failed", "refunded"
    ]
    param_names = ["order", "order_id"]

    while True:
        if camp.get("status") != "active":
            print(f"Campaign no longer active – clearing order {order_id}")
            camp["last_order_id"] = None
            camp["_order_check_time"] = None
            return

        for param in param_names:
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    r = await client.post(api_url, data={
                        "key": api_key,
                        "action": "status",
                        param: order_id
                    })
                    if r.status_code == 200:
                        data = r.json() if r.text.strip() else {}
                        status = (
                            data.get("status") or
                            data.get("order_status") or
                            data.get("state") or
                            data.get("order_state") or
                            (data.get("data") or {}).get("status") or ""
                        ).strip().lower()
                        print(f"🔍 Order {order_id} status: {status}")
                        if status in completed_statuses:
                            print(f"✅ Order {order_id} finished – continuing campaign.")
                            camp["last_order_id"] = None
                            camp["_order_check_time"] = None
                            return
                        else:
                            break
            except Exception as e:
                print(f"⚠️ Error checking order {order_id}: {e}")
        await asyncio.sleep(60)
    print(f"⚠️ Max retries reached for order {order_id}. Auto‑clearing order – campaign will continue.")
    camp["last_order_id"] = None
    camp["_order_check_time"] = None

# ---------- AI Planner Helper ----------
def estimate_completion_time(target, min_v, max_v, interval_min, vibe, smart_boost, timezone_offset=0):
    import random, math, time
    current = 0
    virtualTime = time.time()
    stealthPhase = random.uniform(0, 4)
    MAX_DRIPS = 500
    def activityMultiplier(ts, tzOff, sp):
        local = datetime.fromtimestamp(ts) + timedelta(hours=tzOff + sp)
        hour = local.hour + local.minute/60.0
        peak1 = math.exp(-((hour - 11.5)**2)/(2*3**2))
        peak2 = math.exp(-((hour - 20.5)**2)/(2*3**2))
        act = 0.3 + 1.2 * max(peak1, peak2)
        wd = local.weekday()
        wfactors = [1.0,1.0,1.0,1.0,1.10,1.15,0.80]
        act *= wfactors[wd]
        return max(0.2, min(1.5, act))
    def organicViewCurve(progress):
        center, sigma = 0.6, 0.2
        bell = math.exp(-((progress - center)**2)/(2*sigma**2))
        noise = random.gauss(0, 0.12)
        return max(0.05, min(1.0, bell + noise))
    nextBoostTime = None
    boostViewsLeft = 0
    for _ in range(MAX_DRIPS):
        if current >= target:
            break
        remaining = target - current
        if nextBoostTime and virtualTime >= nextBoostTime:
            boost = boostViewsLeft
            if remaining >= 100:
                boost = max(100, min(boost, remaining))
            else:
                boost = 100
            current += boost
            virtualTime += 0
            nextBoostTime = None
            continue
        progress = current/target if target>0 else 0
        vf = organicViewCurve(progress)
        views = min_v + (max_v - min_v) * vf
        tod = activityMultiplier(virtualTime, timezone_offset, stealthPhase)
        views *= tod
        elapsedH = (virtualTime - time.time())/3600.0
        expected = min(1.0, elapsedH/24.0)
        if progress < expected:
            views *= 1.0 + min(0.3, (expected-progress)*0.5)
        elif progress > expected+0.05:
            views *= max(0.8, 1.0-(progress-expected)*0.5)
        if vibe=='viral' and 0.3<progress<0.8:
            views *= random.uniform(1.05,1.15)
        elif vibe=='stealth' and random.random()<0.3:
            views *= 0.85
        views = int(round(views))
        if remaining >= 100:
            views = max(100, min(views, remaining))
        else:
            views = 100
        current += views
        intervalSec = interval_min * 60
        intervalSec /= max(tod, 0.3)
        if vibe=='viral':
            intervalSec *= random.uniform(0.9,1.1)
        elif vibe=='stealth':
            intervalSec *= random.uniform(1.1,1.4)
        intervalSec *= random.uniform(0.85,1.15)
        intervalSec = max(60, min(10800, intervalSec))
        virtualTime += intervalSec
        if smart_boost and remaining>300 and random.random()<0.04:
            boost = int(views * random.uniform(0.3,0.7))
            if boost>=100:
                nextBoostTime = virtualTime + random.uniform(300,900)
                boostViewsLeft = boost
    total_seconds = virtualTime - time.time()
    return total_seconds / 3600.0

# ---------- AI Plan Endpoint ----------
@app.post("/ai-plan")
async def ai_plan(request: dict, token: str = Depends(verify_token)):
    target = int(request.get("target_views", 1000))
    duration_minutes = int(request.get("duration_minutes", 60))
    safety_speed = float(request.get("safety_speed", 50))
    timezone_offset = int(request.get("timezone_offset", 0))
    max_hours = max(0.5, duration_minutes / 60.0)

    vibes = ["stealth", "hybrid", "viral"]
    min_views_range = [100, 200, 300, 500, 800]
    max_views_range = [300, 500, 800, 1200, 2000, 3000]
    interval_range = [20, 30, 45, 60, 90, 120]
    boost_options = [False, True]

    best = None
    best_score = -1e9
    safety_weight = 1 - (safety_speed / 100.0)
    speed_weight = safety_speed / 100.0

    for vibe in vibes:
        for min_v in min_views_range:
            for max_v in max_views_range:
                if max_v <= min_v: continue
                for interval in interval_range:
                    for smart_boost in boost_options:
                        est_hours = estimate_completion_time(target, min_v, max_v, interval, vibe, smart_boost, timezone_offset)
                        if est_hours > max_hours + 2:
                            continue
                        safety_score = 0.0
                        if vibe == "stealth":
                            safety_score += 50
                        elif vibe == "hybrid":
                            safety_score += 30
                        else:
                            safety_score += 10
                        safety_score += min(40, interval / 120 * 40)
                        ratio = min_v / max_v if max_v>0 else 1
                        safety_score += (1 - ratio) * 20
                        if not smart_boost:
                            safety_score += 10
                        safety_score = min(100, safety_score)
                        speed_score = max(0, min(100, (1 - est_hours / max_hours) * 100)) if max_hours>0 else 0
                        score = safety_weight * safety_score + speed_weight * speed_score
                        if est_hours < max_hours * 0.3:
                            score *= 0.7
                        if score > best_score:
                            best_score = score
                            best = {
                                "vibe": vibe,
                                "min_views": min_v,
                                "max_views": max_v,
                                "interval_minutes": interval,
                                "smart_boost": smart_boost,
                                "estimated_hours": round(est_hours, 1),
                                "risk_level": "Safe" if safety_score >= 70 else "Moderate" if safety_score >= 40 else "Risky" if safety_score >= 20 else "Botted",
                                "explanation": f"Using {vibe} pattern, views {min_v}-{max_v}, every ~{interval} min, {'with' if smart_boost else 'without'} smart boost. Estimated completion in {est_hours:.1f}h."
                            }
    if best is None:
        best = {
            "vibe": "hybrid",
            "min_views": 100,
            "max_views": 500,
            "interval_minutes": 45,
            "smart_boost": True,
            "estimated_hours": 48,
            "risk_level": "Moderate",
            "explanation": "Default plan – increase duration or reduce target."
        }
    return best

# ---------- Main Drip Loop (database version) ----------
async def drip(cid: str):
    if cid not in drip_locks:
        drip_locks[cid] = asyncio.Lock()
    async with drip_locks[cid]:
        row = await database.fetch_one(select(campaigns_table).where(campaigns_table.c.id == cid))
        if not row or row["status"] != "active":
            return
        camp = dict(row)
        while True:
            row = await database.fetch_one(select(campaigns_table).where(campaigns_table.c.id == cid))
            if not row or row["status"] != "active":
                break
            camp = dict(row)
            if camp["current"] >= camp["target"]:
                await database.execute(campaigns_table.update().where(campaigns_table.c.id == cid).values(status="completed", next_drip=0))
                await manager.broadcast({"type": "campaign_completed", "campaign_id": cid})
                break
            if camp.get("last_order_id"):
                camp["_order_check_time"] = time.time()
                await database.execute(campaigns_table.update().where(campaigns_table.c.id == cid).values(_order_check_time=camp["_order_check_time"]))
                await wait_for_order_completion(camp["api_url"], camp["api_key"], camp["last_order_id"], camp)
                await database.execute(campaigns_table.update().where(campaigns_table.c.id == cid).values(last_order_id=None, _order_check_time=None))
                row = await database.fetch_one(select(campaigns_table).where(campaigns_table.c.id == cid))
                if not row: break
                camp = dict(row)
            now = time.time()
            sleep_time = max(0, camp.get("next_drip", now) - now)
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)
            row = await database.fetch_one(select(campaigns_table).where(campaigns_table.c.id == cid))
            if not row or row["status"] != "active":
                break
            camp = dict(row)
            scheduler = AdaptiveDripScheduler(camp)
            views = scheduler.determine_views()
            if views == 0:
                wait_sec = scheduler.determine_interval(0)
                await database.execute(campaigns_table.update().where(campaigns_table.c.id == cid).values(next_drip=time.time() + wait_sec))
                continue
            remaining = camp["target"] - camp["current"]
            views = min(views, remaining)
            if views <= 0:
                views = max(1, camp.get("min_views", 100))
            try:
                start_order = time.time()
                async with httpx.AsyncClient(timeout=20.0) as client:
                    r = await client.post(
                        camp["api_url"],
                        data={
                            "key": camp["api_key"],
                            "action": "add",
                            "service": camp["service_id"],
                            "link": camp["link"],
                            "quantity": views
                        }
                    )
                    if r.status_code in [200, 201]:
                        resp = r.json() if r.text.strip() else {}
                        if not resp.get("error"):
                            new_current = camp["current"] + views
                            new_drip_count = camp.get("drip_count", 0) + 1
                            order_id = resp.get("order") or resp.get("order_id")
                            history = camp.get("history", [])
                            history.append({
                                "views": views,
                                "total": new_current,
                                "time": datetime.now().isoformat(),
                                "drip": new_drip_count
                            })
                            if len(history) > 50:
                                history = history[-50:]
                            update_data = {
                                "current": new_current,
                                "last_views": views,
                                "drip_count": new_drip_count,
                                "history": history,
                                "last_order_duration": time.time() - start_order,
                                "next_drip": time.time() + scheduler.determine_interval(views)
                            }
                            if order_id:
                                update_data["last_order_id"] = int(order_id)
                                update_data["_order_check_time"] = time.time()
                            else:
                                update_data["last_order_id"] = None
                            await database.execute(campaigns_table.update().where(campaigns_table.c.id == cid).values(**update_data))
                            await manager.broadcast({
                                "type": "drip_event",
                                "campaign_id": cid,
                                "views": views,
                                "total": new_current
                            })
                            continue
            except Exception as e:
                print(f"Drip error for {cid}: {e}")
            await database.execute(campaigns_table.update().where(campaigns_table.c.id == cid).values(next_drip=time.time() + 120))
            await asyncio.sleep(60)
    if cid in active_tasks:
        del active_tasks[cid]
    if cid in drip_locks:
        del drip_locks[cid]

# ---------- Serve Frontend ----------
@app.get("/")
async def serve_frontend():
    return FileResponse("index.html")

# ---------- API Endpoints (all from your original, converted to DB) ----------
@app.get("/get-api")
async def get_api(token: str = Depends(verify_token)):
    return {
        "url": api_config.get("url", ""),
        "key": api_config.get("key", ""),
        "balance": api_config.get("balance", "0"),
        "verified": api_verified
    }

@app.post("/disconnect-api")
async def disconnect(token: str = Depends(verify_token)):
    global api_verified
    api_verified = False
    api_config["url"] = ""
    api_config["key"] = ""
    api_config["balance"] = "0"
    return {"status": "disconnected"}

@app.post("/test-api")
async def test_api(config: APIConfig, token: str = Depends(verify_token)):
    global api_verified, api_config
    url = config.api_url.strip().rstrip('/')
    if not url.startswith("http"):
        url = "https://" + url
    key = config.api_key.strip()
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            r = await client.post(url, data={"key": key, "action": "balance"})
            data = r.json() if r.text.strip() else {}
            if data.get("error"):
                raise HTTPException(status_code=400, detail="Invalid API key or connection failed")
            balance = data.get("balance") or (data.get("data") or {}).get("balance")
            if balance is None:
                raise HTTPException(status_code=400, detail="Invalid API key or connection failed")
            api_verified = True
            api_config["url"] = url
            api_config["key"] = key
            api_config["balance"] = str(balance)
            return {"status": "success", "balance": api_config["balance"], "verified": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/launch")
async def launch(config: CampaignConfig, token: str = Depends(verify_token)):
    if not api_verified:
        raise HTTPException(status_code=400, detail="Please verify your API connection first")
    cid = f"camp_{int(time.time())}_{random.randint(1000, 9999)}"
    existing = await database.fetch_one(select(campaigns_table).where(campaigns_table.c.id == cid))
    if existing:
        cid = f"camp_{int(time.time())}_{random.randint(10000, 99999)}"
    min_views = max(config.min_views, 100)
    project = config.project.strip() or "default"
    velocity_target = config.velocity_target_hours
    if config.finish_hours and config.finish_hours > 0:
        velocity_target = config.finish_hours
    await database.execute(campaigns_table.insert().values(
        id=cid,
        api_url=config.api_url.strip(),
        api_key=config.api_key.strip(),
        service_id=config.service_id,
        link=config.link,
        target=config.target,
        min_views=min_views,
        max_views=config.max_views,
        vibe=config.vibe,
        timezone_offset=config.timezone_offset,
        drip_interval_minutes=config.drip_interval_minutes,
        current=0,
        last_views=0,
        next_drip=time.time(),
        status="active",
        history=[],
        last_order_id=None,
        last_order_status=None,
        drip_count=0,
        created=datetime.now(),
        safe_mode=config.safe_mode,
        safety_factor=config.safety_factor,
        smart_boost=config.smart_boost,
        project=project,
        velocity_target_hours=velocity_target,
        last_order_duration=None,
        recent_drips=[],
        stealth_phase=random.uniform(0, 4),
        social_boost_scheduled=None,
        social_boost_views=None,
        _order_check_time=None,
    ))
    if cid in active_tasks:
        active_tasks[cid].cancel()
        del active_tasks[cid]
    task = asyncio.create_task(drip(cid))
    active_tasks[cid] = task
    await manager.broadcast({"type": "campaign_launched", "campaign_id": cid})
    return {"status": "launched", "campaign_id": cid}

@app.post("/stop/{cid}")
async def stop(cid: str, token: str = Depends(verify_token)):
    camp = await database.fetch_one(select(campaigns_table).where(campaigns_table.c.id == cid))
    if not camp:
        raise HTTPException(status_code=404)
    await database.execute(campaigns_table.update().where(campaigns_table.c.id == cid).values(status="stopped"))
    if cid in active_tasks:
        active_tasks[cid].cancel()
        del active_tasks[cid]
    return {"status": "stopped"}

@app.post("/pause/{cid}")
async def pause(cid: str, token: str = Depends(verify_token)):
    camp = await database.fetch_one(select(campaigns_table).where(campaigns_table.c.id == cid))
    if not camp:
        raise HTTPException(status_code=404)
    await database.execute(campaigns_table.update().where(campaigns_table.c.id == cid).values(status="paused"))
    if cid in active_tasks:
        active_tasks[cid].cancel()
        del active_tasks[cid]
    return {"status": "paused"}

@app.post("/resume/{cid}")
async def resume(cid: str, token: str = Depends(verify_token)):
    camp = await database.fetch_one(select(campaigns_table).where(campaigns_table.c.id == cid))
    if not camp or camp["status"] != "paused" or camp["current"] >= camp["target"]:
        raise HTTPException(status_code=404)
    await database.execute(campaigns_table.update().where(campaigns_table.c.id == cid).values(status="active"))
    if cid not in active_tasks:
        active_tasks[cid] = asyncio.create_task(drip(cid))
    return {"status": "resumed"}

@app.delete("/campaign/{cid}")
async def delete(cid: str, token: str = Depends(verify_token)):
    camp = await database.fetch_one(select(campaigns_table).where(campaigns_table.c.id == cid))
    if not camp:
        raise HTTPException(status_code=404)
    if cid in active_tasks:
        active_tasks[cid].cancel()
        del active_tasks[cid]
    if cid in drip_locks:
        del drip_locks[cid]
    await database.execute(campaigns_table.delete().where(campaigns_table.c.id == cid))
    return {"status": "deleted"}

@app.post("/clear-completed")
async def clear_completed(token: str = Depends(verify_token)):
    to_delete = await database.fetch_all(select(campaigns_table.c.id).where(campaigns_table.c.status.in_(["completed", "stopped"])))
    deleted = 0
    for row in to_delete:
        cid = row["id"]
        if cid in active_tasks:
            active_tasks[cid].cancel()
            del active_tasks[cid]
        if cid in drip_locks:
            del drip_locks[cid]
        await database.execute(campaigns_table.delete().where(campaigns_table.c.id == cid))
        deleted += 1
    return {"deleted": deleted}

@app.get("/state")
async def state(project: Optional[str] = Query(None), token: str = Depends(verify_token)):
    query = select(campaigns_table).where(campaigns_table.c.status != "deleted")
    if project and project != "all":
        query = query.where(campaigns_table.c.project == project)
    rows = await database.fetch_all(query)
    result = {}
    for row in rows:
        camp = dict(row)
        camp.pop("api_key", None)
        camp["progress"] = round((camp["current"] / camp["target"]) * 100, 1) if camp["target"] else 0
        result[camp["id"]] = camp
    return result

@app.get("/projects")
async def get_projects(token: str = Depends(verify_token)):
    rows = await database.fetch_all(select(campaigns_table.c.project).distinct().where(campaigns_table.c.status != "deleted"))
    projects = [row["project"] for row in rows]
    return sorted(projects)

@app.get("/analytics")
async def analytics(token: str = Depends(verify_token)):
    rows = await database.fetch_all(select(campaigns_table))
    timeline = defaultdict(int)
    total_views = 0
    pattern_stats = defaultdict(lambda: {"total": 0, "completed": 0})
    for row in rows:
        c = dict(row)
        vibe = c.get("vibe", "hybrid")
        pattern_stats[vibe]["total"] += 1
        if c["status"] == "completed":
            pattern_stats[vibe]["completed"] += 1
        for h in c.get("history", []):
            try:
                hour = datetime.fromisoformat(h["time"]).hour
                timeline[hour] += h["views"]
                total_views += h["views"]
            except:
                pass
    return {
        "timeline": [{"hour": h, "views": v} for h, v in sorted(timeline.items())],
        "pattern_performance": {
            k: round((v["completed"]/v["total"])*100, 1) if v["total"] > 0 else 0
            for k, v in pattern_stats.items()
        },
        "total_views": total_views
    }

@app.post("/bulk-resume")
async def bulk_resume(token: str = Depends(verify_token)):
    rows = await database.fetch_all(select(campaigns_table).where(campaigns_table.c.status == "paused").where(campaigns_table.c.current < campaigns_table.c.target))
    resumed = 0
    for row in rows:
        cid = row["id"]
        await database.execute(campaigns_table.update().where(campaigns_table.c.id == cid).values(status="active"))
        if cid not in active_tasks:
            active_tasks[cid] = asyncio.create_task(drip(cid))
        resumed += 1
    return {"ok": True, "resumed": resumed}

@app.post("/bulk-pause")
async def bulk_pause(token: str = Depends(verify_token)):
    rows = await database.fetch_all(select(campaigns_table.c.id).where(campaigns_table.c.status == "active"))
    paused = 0
    for row in rows:
        cid = row["id"]
        await database.execute(campaigns_table.update().where(campaigns_table.c.id == cid).values(status="paused"))
        if cid in active_tasks:
            active_tasks[cid].cancel()
            del active_tasks[cid]
        paused += 1
    return {"ok": True, "paused": paused}

@app.post("/bulk-stop")
async def bulk_stop(token: str = Depends(verify_token)):
    rows = await database.fetch_all(select(campaigns_table.c.id).where(campaigns_table.c.status.in_(["active", "paused"])))
    stopped = 0
    for row in rows:
        cid = row["id"]
        await database.execute(campaigns_table.update().where(campaigns_table.c.id == cid).values(status="stopped"))
        if cid in active_tasks:
            active_tasks[cid].cancel()
            del active_tasks[cid]
        stopped += 1
    return {"ok": True, "stopped": stopped}

@app.post("/force-complete/{cid}")
async def force_complete(cid: str, token: str = Depends(verify_token)):
    camp = await database.fetch_one(select(campaigns_table).where(campaigns_table.c.id == cid))
    if not camp:
        raise HTTPException(status_code=404)
    if camp.get("last_order_id"):
        await database.execute(campaigns_table.update().where(campaigns_table.c.id == cid).values(last_order_id=None, _order_check_time=None, next_drip=time.time()))
        await manager.broadcast({"type": "force_complete", "campaign_id": cid})
        return {"status": "completed", "message": "Order manually completed"}
    return {"status": "no_pending_order", "message": "No order to complete"}

@app.get("/health")
async def health_check():
    return {"status": "ok", "campaigns": len(active_tasks), "api_verified": api_verified}

# ---------- Lifespan ----------
@app.on_event("startup")
async def startup():
    await database.connect()
    rows = await database.fetch_all(select(campaigns_table).where(campaigns_table.c.status == "active").where(campaigns_table.c.current < campaigns_table.c.target))
    recovered = 0
    for row in rows:
        cid = row["id"]
        if cid not in active_tasks:
            active_tasks[cid] = asyncio.create_task(drip(cid))
            recovered += 1
    if recovered:
        print(f"🔄 Recovered {recovered} active campaigns")

@app.on_event("shutdown")
async def shutdown():
    await database.disconnect()

if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
