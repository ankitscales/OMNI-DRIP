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

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import FileResponse
from pydantic import BaseModel
import httpx
import uvicorn

# ---------- DRIP ENGINE ----------
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

# ---------- Persistence ----------
DATA_FILE = "omni_data.json"
api_verified = False
api_config = {"url": "", "key": "", "balance": "0"}
campaigns: Dict[str, dict] = {}
active_tasks: Dict[str, asyncio.Task] = {}
drip_locks: Dict[str, asyncio.Lock] = {}

def load():
    global api_config, campaigns, api_verified
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE) as f:
                d = json.load(f)
                api_config = d.get("api", {"url": "", "key": "", "balance": "0"})
                saved = d.get("campaigns", {})
                api_verified = d.get("api_verified", False)
                for cid, c in saved.items():
                    if "target" in c and "current" in c:
                        c.setdefault("last_order_id", None)
                        c.setdefault("last_order_status", None)
                        c.setdefault("safe_mode", False)
                        c.setdefault("safety_factor", 0.7)
                        c.setdefault("drip_count", 0)
                        c.setdefault("smart_boost", False)
                        c.setdefault("project", "default")
                        c.setdefault("vibe", "hybrid")
                        c.setdefault("timezone_offset", 0)
                        c.setdefault("velocity_target_hours", 24.0)
                        c.setdefault("last_order_duration", None)
                        c.setdefault("recent_drips", [])
                        c.setdefault("stealth_phase", random.uniform(0, 4))
                        c.setdefault("social_boost_scheduled", None)
                        c.setdefault("social_boost_views", None)
                        campaigns[cid] = c
        except Exception as e:
            print(f"Load error: {e}")
            campaigns = {}

def save():
    try:
        clean = {}
        for cid, c in campaigns.items():
            clean[cid] = {k: v for k, v in c.items() if isinstance(v, (int, float, str, bool, list, dict, type(None)))}
        data = {"api": api_config, "campaigns": clean, "api_verified": api_verified}
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, DATA_FILE)
    except Exception as e:
        print(f"Save error: {e}")

load()

# ---------- FIXED: wait_for_order_completion (tries both 'order' and 'order_id') ----------
async def wait_for_order_completion(api_url: str, api_key: str, order_id: int, camp: dict) -> None:
    """Check every 60 seconds indefinitely until order completes or campaign becomes inactive."""
    completed_statuses = [
        "completed", "success", "finished", "done", "complete",
        "partial", "canceled", "error", "failed", "refunded"
    ]
    param_names = ["order", "order_id"]

    while True:
        # Stop if campaign is no longer active
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
                            break  # status read, will retry after 60s
            except Exception as e:
                print(f"⚠️ Error checking order {order_id}: {e}")

        # Wait 60 seconds before next check
        await asyncio.sleep(60)
    
    # After max retries, force clear so campaign continues
    print(f"⚠️ Max retries reached for order {order_id}. Auto‑clearing order – campaign will continue.")
    camp["last_order_id"] = None
    camp["_order_check_time"] = None

# ---------- Manual Force Complete (emergency) ----------
@app.post("/force-complete/{cid}")
async def force_complete(cid: str, token: str = Depends(verify_token)):
    if cid not in campaigns:
        raise HTTPException(status_code=404, detail="Campaign not found")
    camp = campaigns[cid]
    if camp.get("last_order_id"):
        camp["last_order_id"] = None
        camp["_order_check_time"] = None
        camp["next_drip"] = time.time()
        save()
        await manager.broadcast({"type": "force_complete", "campaign_id": cid})
        return {"status": "completed", "message": "Order manually completed"}
    return {"status": "no_pending_order", "message": "No order to complete"}

# ---------- Main campaign loop (drip) ----------
async def drip(cid):
    if cid not in drip_locks:
        drip_locks[cid] = asyncio.Lock()

    async with drip_locks[cid]:
        camp = campaigns.get(cid)
        if not camp or camp.get("status") != "active":
            return

        while True:
            camp = campaigns.get(cid)
            if not camp or camp.get("status") != "active":
                break

            if camp["current"] >= camp["target"]:
                camp["status"] = "completed"
                camp["next_drip"] = 0
                save()
                await manager.broadcast({"type": "campaign_completed", "campaign_id": cid})
                break

            # Wait for pending order to complete (1‑minute loop)
            if camp.get("last_order_id"):
                camp["_order_check_time"] = time.time()
                save()
                await wait_for_order_completion(
                    camp["api_url"],
                    camp["api_key"],
                    camp["last_order_id"],
                    camp
                )
                camp["last_order_id"] = None
                camp["_order_check_time"] = None
                save()

            now = time.time()
            sleep_time = max(0, camp.get("next_drip", now) - now)
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

            camp = campaigns.get(cid)
            if not camp or camp.get("status") != "active":
                break
            if camp["current"] >= camp["target"]:
                continue

            scheduler = AdaptiveDripScheduler(camp)
            views = scheduler.determine_views()

            if views == 0:
                wait_sec = scheduler.determine_interval(0)
                camp["next_drip"] = time.time() + wait_sec
                save()
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
                            camp["current"] += views
                            camp["last_views"] = views
                            camp["drip_count"] = camp.get("drip_count", 0) + 1

                            order_id = resp.get("order") or resp.get("order_id")
                            if order_id:
                                camp["last_order_id"] = int(order_id)
                                camp["_order_check_time"] = time.time()
                            else:
                                # If no order ID, assume completed immediately
                                camp["last_order_id"] = None

                            camp["history"].append({
                                "views": views,
                                "total": camp["current"],
                                "time": datetime.now().isoformat(),
                                "drip": camp["drip_count"]
                            })

                            if len(camp["history"]) > 50:
                                camp["history"] = camp["history"][-50:]

                            await manager.broadcast({
                                "type": "drip_event",
                                "campaign_id": cid,
                                "views": views,
                                "total": camp["current"]
                            })

                            camp["last_order_duration"] = time.time() - start_order
                            wait_sec = scheduler.determine_interval(views)
                            camp["next_drip"] = time.time() + wait_sec
                            save()
                            continue
            except Exception as e:
                print(f"Drip error for {cid}: {e}")
                camp["last_order_duration"] = 999

            camp["next_drip"] = time.time() + 120
            save()
            await asyncio.sleep(60)

    if cid in active_tasks:
        del active_tasks[cid]
    if cid in drip_locks:
        del drip_locks[cid]
    save()


# ---------- All other endpoints (unchanged) ----------
@app.get("/")
async def serve_frontend():
    return FileResponse("index.html")

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
    save()
    return {"status": "disconnected"}

@app.post("/test-api")
async def test_api(config: APIConfig, token: str = Depends(verify_token)):
    global api_verified
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
            save()
            return {"status": "success", "balance": api_config["balance"], "verified": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid API key or connection failed")

@app.post("/launch")
async def launch(config: CampaignConfig, token: str = Depends(verify_token)):
    if not api_verified:
        raise HTTPException(status_code=400, detail="Please verify your API connection first")

    cid = f"camp_{int(time.time())}_{random.randint(1000, 9999)}"
    if cid in campaigns:
        cid = f"camp_{int(time.time())}_{random.randint(10000, 99999)}"

    min_views = max(config.min_views, 100)
    project = config.project.strip() or "default"

    velocity_target = config.velocity_target_hours
    if config.finish_hours and config.finish_hours > 0:
        velocity_target = config.finish_hours

    campaigns[cid] = {
        "id": cid,
        "api_url": config.api_url.strip(),
        "api_key": config.api_key.strip(),
        "service_id": config.service_id,
        "link": config.link,
        "target": config.target,
        "min_views": min_views,
        "max_views": config.max_views,
        "vibe": config.vibe,
        "timezone_offset": config.timezone_offset,
        "drip_interval_minutes": config.drip_interval_minutes,
        "current": 0,
        "last_views": 0,
        "next_drip": time.time(),
        "status": "active",
        "history": [],
        "last_order_id": None,
        "last_order_status": None,
        "drip_count": 0,
        "created": datetime.now().isoformat(),
        "safe_mode": config.safe_mode,
        "safety_factor": config.safety_factor,
        "smart_boost": config.smart_boost,
        "project": project,
        "velocity_target_hours": velocity_target,
        "last_order_duration": None,
        "recent_drips": [],
        "stealth_phase": random.uniform(0, 4),
        "social_boost_scheduled": None,
        "social_boost_views": None
    }
    save()

    if cid in active_tasks:
        active_tasks[cid].cancel()
        del active_tasks[cid]

    task = asyncio.create_task(drip(cid))
    active_tasks[cid] = task
    await manager.broadcast({"type": "campaign_launched", "campaign_id": cid})
    return {"status": "launched", "campaign_id": cid}

@app.post("/stop/{cid}")
async def stop(cid: str, token: str = Depends(verify_token)):
    if cid in campaigns:
        campaigns[cid]["status"] = "stopped"
        if cid in active_tasks:
            active_tasks[cid].cancel()
            del active_tasks[cid]
        save()
        return {"status": "stopped"}
    raise HTTPException(status_code=404)

@app.post("/pause/{cid}")
async def pause(cid: str, token: str = Depends(verify_token)):
    if cid in campaigns:
        campaigns[cid]["status"] = "paused"
        if cid in active_tasks:
            active_tasks[cid].cancel()
            del active_tasks[cid]
        save()
        return {"status": "paused"}
    raise HTTPException(status_code=404)

@app.post("/resume/{cid}")
async def resume(cid: str, token: str = Depends(verify_token)):
    if cid in campaigns:
        camp = campaigns[cid]
        if camp.get("status") == "paused" and camp["current"] < camp["target"]:
            camp["status"] = "active"
            if cid not in active_tasks:
                active_tasks[cid] = asyncio.create_task(drip(cid))
            save()
            return {"status": "resumed"}
    raise HTTPException(status_code=404)

@app.delete("/campaign/{cid}")
async def delete(cid: str, token: str = Depends(verify_token)):
    if cid in campaigns:
        if cid in active_tasks:
            active_tasks[cid].cancel()
            del active_tasks[cid]
        if cid in drip_locks:
            del drip_locks[cid]
        del campaigns[cid]
        save()
        return {"status": "deleted"}
    raise HTTPException(status_code=404)

@app.post("/clear-completed")
async def clear_completed(token: str = Depends(verify_token)):
    to_delete = [cid for cid, c in campaigns.items() if c.get("status") in ("completed", "stopped")]
    for cid in to_delete:
        if cid in active_tasks:
            active_tasks[cid].cancel()
            del active_tasks[cid]
        if cid in drip_locks:
            del drip_locks[cid]
        del campaigns[cid]
    save()
    return {"deleted": len(to_delete)}

@app.get("/state")
async def state(project: Optional[str] = Query(None), token: str = Depends(verify_token)):
    result = {}
    for cid, c in campaigns.items():
        if c.get("status") != "deleted":
            if project and project != "all" and c.get("project", "default") != project:
                continue
            camp = c.copy()
            camp.pop("api_key", None)
            camp["progress"] = round((c["current"] / c["target"]) * 100, 1) if c["target"] else 0
            result[cid] = camp
    return result

@app.get("/projects")
async def get_projects(token: str = Depends(verify_token)):
    projects = set()
    for c in campaigns.values():
        if c.get("status") != "deleted":
            projects.add(c.get("project", "default"))
    return sorted(list(projects))

@app.get("/analytics")
async def analytics(token: str = Depends(verify_token)):
    timeline = defaultdict(int)
    total_views = 0
    pattern_stats = defaultdict(lambda: {"total": 0, "completed": 0})
    for c in campaigns.values():
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
    count = 0
    for cid, c in campaigns.items():
        if c.get("status") == "paused" and c["current"] < c["target"]:
            c["status"] = "active"
            if cid not in active_tasks:
                active_tasks[cid] = asyncio.create_task(drip(cid))
            count += 1
    save()
    return {"ok": True, "resumed": count}

@app.post("/bulk-pause")
async def bulk_pause(token: str = Depends(verify_token)):
    count = 0
    for cid, c in campaigns.items():
        if c.get("status") == "active":
            c["status"] = "paused"
            if cid in active_tasks:
                active_tasks[cid].cancel()
                del active_tasks[cid]
            count += 1
    save()
    return {"ok": True, "paused": count}

@app.post("/bulk-stop")
async def bulk_stop(token: str = Depends(verify_token)):
    count = 0
    for cid, c in campaigns.items():
        if c.get("status") in ("active", "paused"):
            c["status"] = "stopped"
            if cid in active_tasks:
                active_tasks[cid].cancel()
                del active_tasks[cid]
            count += 1
    save()
    return {"ok": True, "stopped": count}

@app.get("/health")
async def health_check():
    return {"status": "ok", "campaigns": len(campaigns), "api_verified": api_verified}

@app.on_event("startup")
async def startup():
    recovered = 0
    for cid, c in list(campaigns.items()):
        if c.get("status") == "active" and c["current"] < c["target"]:
            if cid not in active_tasks:
                active_tasks[cid] = asyncio.create_task(drip(cid))
                recovered += 1
    if recovered:
        print(f"🔄 Recovered {recovered} active campaigns")

if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)