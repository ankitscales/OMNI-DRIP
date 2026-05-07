"""
drip_engine.py
Ultra‑Realistic Organic Drip Scheduler for Instagram
=====================================================
- All drips (including social boosts) are ≥100 views until the final drip.
- Natural bell‑shaped curve with Gaussian noise.
- Time‑of‑day & weekday activity multiplier.
- Adaptive pacing & order‑speed feedback.
- Cooldown prevention.
"""

import math
import random
import time
from datetime import datetime, timedelta

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

    # ---------- Activity multiplier ----------
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

    # ---------- Organic view curve ----------
    def _organic_view_curve(self, progress: float) -> float:
        center, sigma = 0.6, 0.2
        bell = math.exp(-((progress - center) ** 2) / (2 * sigma ** 2))
        noise = random.gauss(0, 0.12)
        return max(0.05, min(1.0, bell + noise))

    # ---------- Social boost helpers ----------
    def _should_social_boost(self) -> bool:
        if not self.camp.get("smart_boost", False):
            return False
        remaining = self.camp["target"] - self.camp["current"]
        if remaining < 300:
            return False
        return random.random() < random.uniform(0.02, 0.08)

    def _compute_social_boost_views(self, last_views: int) -> int:
        return int(last_views * random.uniform(0.3, 0.7))

    # ---------- Main decision methods ----------
    def determine_views(self) -> int:
        camp = self.camp
        target = camp["target"]
        current = camp.get("current", 0)
        remaining = target - current
        if remaining <= 0:
            return 0

        # 1. Pending social boost?
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

        # 2. Base views from organic curve
        progress = current / target if target > 0 else 0
        vf = self._organic_view_curve(progress)
        min_v = max(camp.get("min_views", 100), 100)
        max_v = camp.get("max_views", 600)
        views = min_v + (max_v - min_v) * vf

        # 3. Time of day
        now = datetime.utcnow()
        tod = self.activity_multiplier(
            now, camp.get("timezone_offset", 0), camp["stealth_phase"]
        )
        views *= tod

        # 4. Pacing controller
        elapsed_hours = (time.time() - self._campaign_start_time()) / 3600.0
        target_hours = camp.get("velocity_target_hours", 24.0)
        expected = min(1.0, elapsed_hours / target_hours) if target_hours > 0 else 1.0
        if progress < expected:
            views *= 1.0 + min(0.3, (expected - progress) * 0.5)
        elif progress > expected + 0.05:
            views *= max(0.8, 1.0 - (progress - expected) * 0.5)

        # 5. Speed feedback
        if camp.get("last_order_duration") and camp["last_order_duration"] > 1800:
            views *= 0.9

        # 6. Vibe tweaks
        vibe = camp.get("vibe", "hybrid")
        if vibe == "viral" and 0.3 < progress < 0.8:
            views *= random.uniform(1.05, 1.15)
        elif vibe == "stealth" and random.random() < 0.3:
            views *= 0.85

        # 7. Hard bounds
        views = int(round(views))
        if remaining >= 100:
            views = max(100, views)
            views = min(views, remaining)
        else:
            views = 100

        # 8. Schedule social boost
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
        tod = self.activity_multiplier(
            now, camp.get("timezone_offset", 0), camp["stealth_phase"]
        )
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