"""Time-series for link quality (hourly) and battery (daily), kept in a separate file."""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

LQI_KEEP_HOURS = 24 * 14
BATTERY_KEEP_DAYS = 400


class Metrics:
    """Per-device LQI hourly averages and daily battery readings."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.lock = threading.RLock()
        # {ieee: {"lqi": [[hour_ts, sum, count], ...], "bat": [[day_ts, value], ...]}}
        self.series: dict[str, dict[str, list]] = {}
        self.dirty = False

    def load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return
        with self.lock:
            self.series = data if isinstance(data, dict) else {}

    def save(self, force: bool = False) -> None:
        with self.lock:
            if not self.dirty and not force:
                return
            payload = json.dumps(self.series, separators=(",", ":"))
            self.dirty = False
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp, self.path)

    def forget(self, ieee: str) -> None:
        with self.lock:
            if self.series.pop(ieee, None) is not None:
                self.dirty = True

    # ----------------------------------------------------------------- record

    def add_lqi(self, ieee: str, value: Any, now: float | None = None) -> None:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return
        now = now or time.time()
        hour = int(now // 3600 * 3600)
        with self.lock:
            points = self.series.setdefault(ieee, {}).setdefault("lqi", [])
            if points and points[-1][0] == hour:
                points[-1][1] += float(value)
                points[-1][2] += 1
            else:
                points.append([hour, float(value), 1])
                cutoff = hour - LQI_KEEP_HOURS * 3600
                while points and points[0][0] < cutoff:
                    points.pop(0)
            self.dirty = True

    def add_battery(self, ieee: str, value: Any, now: float | None = None) -> float | None:
        """Record a battery reading. Returns the previous day's value if this looks like a replacement."""
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        now = now or time.time()
        day = int(now // 86400 * 86400)
        replaced_from = None
        with self.lock:
            points = self.series.setdefault(ieee, {}).setdefault("bat", [])
            if points and points[-1][0] == day:
                if value - points[-1][1] >= 25:
                    replaced_from = points[-1][1]
                points[-1][1] = float(value)
            else:
                if points and value - points[-1][1] >= 25:
                    replaced_from = points[-1][1]
                points.append([day, float(value)])
                cutoff = day - BATTERY_KEEP_DAYS * 86400
                while points and points[0][0] < cutoff:
                    points.pop(0)
            self.dirty = True
        return replaced_from

    # ---------------------------------------------------------------- summary

    def summary(self, ieee: str, now: float | None = None) -> dict[str, Any]:
        now = now or time.time()
        with self.lock:
            data = self.series.get(ieee) or {}
            lqi = [list(p) for p in data.get("lqi", [])]
            bat = [list(p) for p in data.get("bat", [])]
        out: dict[str, Any] = {}

        if lqi:

            def avg_since(seconds: float, until: float | None = None) -> float | None:
                total = count = 0.0
                for hour, value_sum, n in lqi:
                    if hour >= now - seconds and (until is None or hour < now - until):
                        total += value_sum
                        count += n
                return round(total / count, 1) if count else None

            out["lqi_24h"] = avg_since(86400)
            out["lqi_prev_7d"] = avg_since(8 * 86400, 86400)
            # 7-day sparkline in 4-hour buckets
            buckets: dict[int, list[float]] = {}
            for hour, value_sum, n in lqi:
                if hour >= now - 7 * 86400:
                    key = int(hour // 14400 * 14400)
                    bucket = buckets.setdefault(key, [0.0, 0])
                    bucket[0] += value_sum
                    bucket[1] += n
            out["lqi_spark"] = [[key, round(v[0] / v[1], 1)] for key, v in sorted(buckets.items()) if v[1]]

        if bat:
            out["battery_spark"] = [[day, value] for day, value in bat if day >= now - 120 * 86400]
            out["battery_estimate_days"] = self._battery_estimate(bat, now)
        return out

    @staticmethod
    def _battery_estimate(points: list[list[float]], now: float) -> float | None:
        """Days until the battery reaches 10%, from a linear fit since the last replacement."""
        start = 0
        for index in range(1, len(points)):
            if points[index][1] - points[index - 1][1] >= 25:
                start = index
        window = [p for p in points[start:] if p[0] >= now - 120 * 86400]
        if len(window) < 5 or window[-1][0] - window[0][0] < 14 * 86400:
            return None
        xs = [p[0] / 86400 for p in window]
        ys = [p[1] for p in window]
        mean_x = sum(xs) / len(xs)
        mean_y = sum(ys) / len(ys)
        denom = sum((x - mean_x) ** 2 for x in xs)
        if not denom:
            return None
        slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / denom
        if slope >= -0.02 or ys[0] - ys[-1] < 3:
            return None
        current = ys[-1]
        if current <= 10:
            return 0.0
        return round((current - 10) / -slope, 0)
