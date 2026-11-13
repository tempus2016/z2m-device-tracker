"""Time-series for link quality (hourly and daily), battery (daily) and router reply time (daily), kept in a separate file."""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

LQI_KEEP_HOURS = 24 * 14
LQI_DAILY_KEEP_DAYS = 90
MSG_KEEP_HOURS = 24 * 14
BATTERY_KEEP_DAYS = 400
DRAIN_RECENT_DAYS = 14
HISTORY_RANGES = {"24h": 1, "7d": 7, "30d": 30, "90d": 90}
PING_KEEP_DAYS = 30
PING_CHART_DAYS = 14
# slow_router: the median reply time over the last few days against the median of the two weeks before.
SLOW_RECENT_DAYS = 3
SLOW_BASE_DAYS = 14
SLOW_FACTOR = 3
SLOW_MIN_MS = 150
SLOW_MIN_RECENT = 2
SLOW_MIN_BASE = 5


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


class Metrics:
    """Per-device LQI hourly averages, hourly message counts and daily battery readings."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.lock = threading.RLock()
        # {ieee: {"lqi": [[hour_ts, sum, count], ...], "msg": [[hour_ts, count], ...], "bat": [[day_ts, value], ...],
        #         "lqid": [[day_ts, min, sum, count, max], ...], "ping": [[day_ts, ms or None], ...]}}
        # "ping" holds one nightly reply time per router per day; None means it didn't reply.
        # "lqid" (daily LQI rollups) was added later. Older files lack it; it's rebuilt from the hourly series.
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
            device = self.series.setdefault(ieee, {})
            if "lqid" not in device:
                device["lqid"] = self._daily_from_hourly(device.get("lqi", []))
            points = device.setdefault("lqi", [])
            if points and points[-1][0] == hour:
                points[-1][1] += float(value)
                points[-1][2] += 1
            else:
                points.append([hour, float(value), 1])
                cutoff = hour - LQI_KEEP_HOURS * 3600
                while points and points[0][0] < cutoff:
                    points.pop(0)
            self._add_daily_lqi(device["lqid"], now, float(value))
            self.dirty = True

    @staticmethod
    def _add_daily_lqi(daily: list, now: float, value: float) -> None:
        day = int(now // 86400 * 86400)
        if daily and daily[-1][0] == day:
            entry = daily[-1]
            entry[1] = min(entry[1], value)
            entry[2] += value
            entry[3] += 1
            entry[4] = max(entry[4], value)
            return
        daily.append([day, value, value, 1, value])
        cutoff = day - LQI_DAILY_KEEP_DAYS * 86400
        while daily and daily[0][0] < cutoff:
            daily.pop(0)

    @staticmethod
    def _daily_from_hourly(hourly: list) -> list:
        """Daily rollups rebuilt from hourly averages (used for data recorded before daily rollups existed).

        Min and max here are the lowest and highest hourly average, not single readings.
        """
        days: dict[int, list[float]] = {}
        for hour, value_sum, n in hourly:
            if not n:
                continue
            avg = round(value_sum / n, 1)
            day = int(hour // 86400 * 86400)
            entry = days.get(day)
            if entry is None:
                days[day] = [avg, value_sum, n, avg]
            else:
                entry[0] = min(entry[0], avg)
                entry[1] += value_sum
                entry[2] += n
                entry[3] = max(entry[3], avg)
        return [[day, *days[day]] for day in sorted(days)][-LQI_DAILY_KEEP_DAYS:]

    def add_message(self, ieee: str, now: float | None = None) -> None:
        now = now or time.time()
        hour = int(now // 3600 * 3600)
        with self.lock:
            points = self.series.setdefault(ieee, {}).setdefault("msg", [])
            if points and points[-1][0] == hour:
                points[-1][1] += 1
            else:
                points.append([hour, 1])
                cutoff = hour - MSG_KEEP_HOURS * 3600
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

    def add_ping(self, ieee: str, ms: float | None, now: float | None = None) -> None:
        """Record tonight's reply time for a router (None for no reply). One value per day; a rerun replaces it."""
        if ms is not None and (not isinstance(ms, (int, float)) or isinstance(ms, bool) or ms < 0):
            return
        now = now or time.time()
        day = int(now // 86400 * 86400)
        value = None if ms is None else round(float(ms))
        with self.lock:
            points = self.series.setdefault(ieee, {}).setdefault("ping", [])
            if points and points[-1][0] == day:
                points[-1][1] = value
            else:
                points.append([day, value])
                cutoff = day - PING_KEEP_DAYS * 86400
                while points and points[0][0] <= cutoff:
                    points.pop(0)
            self.dirty = True

    @staticmethod
    def ping_slowdown(points: list[list], now: float) -> dict | None:
        """Median reply time over the last SLOW_RECENT_DAYS days against the SLOW_BASE_DAYS days before.

        Returns {"from": ms, "to": ms} when the router got more than SLOW_FACTOR times slower and is
        above SLOW_MIN_MS, else None. Nights without a reply are left out (the offline check covers those).
        """
        today = int(now // 86400 * 86400)
        recent_start = today - (SLOW_RECENT_DAYS - 1) * 86400
        base_start = recent_start - SLOW_BASE_DAYS * 86400
        recent = [ms for day, ms in points if ms is not None and day >= recent_start and day <= today]
        base = [ms for day, ms in points if ms is not None and base_start <= day < recent_start]
        if len(recent) < SLOW_MIN_RECENT or len(base) < SLOW_MIN_BASE:
            return None
        now_ms, usual_ms = _median(recent), _median(base)
        if now_ms <= SLOW_MIN_MS or now_ms <= SLOW_FACTOR * usual_ms:
            return None
        return {"from": round(usual_ms), "to": round(now_ms)}

    # ---------------------------------------------------------------- summary

    def summary(self, ieee: str, now: float | None = None) -> dict[str, Any]:
        now = now or time.time()
        with self.lock:
            data = self.series.get(ieee) or {}
            lqi = [list(p) for p in data.get("lqi", [])]
            msg = [list(p) for p in data.get("msg", [])]
            bat = [list(p) for p in data.get("bat", [])]
            ping = [list(p) for p in data.get("ping", [])]
        out: dict[str, Any] = {}

        if ping:
            today = int(now // 86400 * 86400)
            out["ping_daily"] = [p for p in ping if p[0] > today - PING_CHART_DAYS * 86400]
            slow = self.ping_slowdown(ping, now)
            if slow:
                out["ping_slow"] = slow

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

        if msg:
            hour_now = int(now // 3600 * 3600)
            out["msg_24h"] = sum(n for hour, n in msg if hour > hour_now - 24 * 3600)
            # Rate over the last 6 hours, counting the current (partial) hour as a fraction.
            recent = [(hour, n) for hour, n in msg if hour > hour_now - 6 * 3600]
            if recent:
                covered = (hour_now - max(recent[0][0], hour_now - 5 * 3600)) / 3600 + max(
                    0.25, (now - hour_now) / 3600
                )
                out["msg_rate_6h"] = round(sum(n for _, n in recent) / covered, 1)
            older = [(hour, n) for hour, n in msg if hour_now - 8 * 86400 <= hour <= hour_now - 86400]
            if len(older) >= 48:
                span_hours = (older[-1][0] - older[0][0]) / 3600 + 1
                out["msg_rate_prev"] = round(sum(n for _, n in older) / span_hours, 1)

        if bat:
            out["battery_spark"] = [[day, value] for day, value in bat if day >= now - 120 * 86400]
            out["battery_estimate_days"] = self._battery_estimate(bat, now)
            drain = self._battery_drain(bat, now)
            if drain:
                out["battery_drain"] = drain
        return out

    @staticmethod
    def _slope(points: list[list[float]]) -> float | None:
        """Least-squares slope in % per day."""
        if len(points) < 2:
            return None
        xs = [p[0] / 86400 for p in points]
        ys = [p[1] for p in points]
        mean_x = sum(xs) / len(xs)
        mean_y = sum(ys) / len(ys)
        denom = sum((x - mean_x) ** 2 for x in xs)
        if not denom:
            return None
        return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / denom

    @classmethod
    def _battery_drain(cls, points: list[list[float]], now: float) -> dict | None:
        """Compare the last 14 days' drain with the device's own rate since the last battery change."""
        start = 0
        for index in range(1, len(points)):
            if points[index][1] - points[index - 1][1] >= 25:
                start = index
        since = points[start:]
        cutoff = now - DRAIN_RECENT_DAYS * 86400
        recent = [p for p in since if p[0] >= cutoff]
        usual = [p for p in since if p[0] < cutoff]
        if len(recent) < 5 or len(usual) < 10 or usual[-1][0] - usual[0][0] < 21 * 86400:
            return None
        recent_slope = cls._slope(recent)
        usual_slope = cls._slope(usual)
        if recent_slope is None or usual_slope is None:
            return None
        recent_rate = -recent_slope
        usual_rate = max(-usual_slope, 0.05)
        dropped = recent[0][1] - recent[-1][1]
        if recent_rate * DRAIN_RECENT_DAYS < 8 or dropped < 6 or recent_rate < 3 * usual_rate:
            return None
        return {
            "days": DRAIN_RECENT_DAYS,
            "dropped": round(dropped, 1),
            "rate": round(recent_rate, 2),
            "usual_rate": round(max(-usual_slope, 0.0), 2),
            "factor": round(recent_rate / usual_rate, 1),
        }

    @classmethod
    def _battery_fit(cls, points: list[list[float]], now: float) -> tuple[float, float, float] | None:
        """Linear fit since the last replacement: (slope in % per day, first value, current value)."""
        start = 0
        for index in range(1, len(points)):
            if points[index][1] - points[index - 1][1] >= 25:
                start = index
        window = [p for p in points[start:] if p[0] >= now - 120 * 86400]
        if len(window) < 5 or window[-1][0] - window[0][0] < 14 * 86400:
            return None
        slope = cls._slope(window)
        if slope is None:
            return None
        return slope, window[0][1], window[-1][1]

    @classmethod
    def _battery_estimate(cls, points: list[list[float]], now: float) -> float | None:
        """Days until the battery reaches 10%, from a linear fit since the last replacement."""
        fit = cls._battery_fit(points, now)
        if fit is None:
            return None
        slope, first, current = fit
        if slope >= -0.02 or first - current < 3:
            return None
        if current <= 10:
            return 0.0
        return round((current - 10) / -slope, 0)

    # ---------------------------------------------------------------- history

    def history(self, ieee: str, range_key: str, now: float | None = None) -> dict[str, Any] | None:
        """LQI and battery series for the device pane's long-range charts. None for an unknown range."""
        days = HISTORY_RANGES.get(range_key)
        if days is None:
            return None
        now = now or time.time()
        start = now - days * 86400
        with self.lock:
            data = self.series.get(ieee) or {}
            hourly = [list(p) for p in data.get("lqi", [])]
            daily = [list(p) for p in data.get("lqid", [])] if "lqid" in data else self._daily_from_hourly(hourly)
            bat = [list(p) for p in data.get("bat", [])]

        in_range = [d for d in daily if d[0] + 86400 > start and d[3]]
        if days <= 7:
            # Short ranges draw hourly averages, plotted at the middle of each hour.
            rows = [h for h in hourly if h[0] + 3600 > start and h[2]]
            line = [[h[0] + 1800, round(h[1] / h[2], 1)] for h in rows]
            total, count = sum(h[1] for h in rows), sum(h[2] for h in rows)
        else:
            line = [[d[0] + 43200, round(d[2] / d[3], 1)] for d in in_range]
            total, count = sum(d[2] for d in in_range), sum(d[3] for d in in_range)
        lqi: dict[str, Any] = {
            "line": line,
            "band": [[d[0], round(d[1], 1), round(d[4], 1)] for d in in_range],
            "avg": round(total / count, 1) if count else None,
            "trend_month": None,
        }
        # Trend needs a few days of daily averages to mean anything.
        trend_points = [[d[0] + 43200, d[2] / d[3]] for d in in_range]
        if len(trend_points) >= 5 and trend_points[-1][0] - trend_points[0][0] >= 4 * 86400:
            slope = self._slope(trend_points)
            if slope is not None:
                lqi["trend_month"] = round(slope * 30, 1)

        # Battery reports are daily, so short ranges still show at least a week.
        bat_days = max(days, 7)
        battery: dict[str, Any] = {
            "start": int(now - bat_days * 86400),
            "points": [[day, value] for day, value in bat if day + 86400 > now - bat_days * 86400],
            "drain_month": None,
            "estimate_days": None,
            "empty_at": None,
        }
        fit = self._battery_fit(bat, now) if bat else None
        if fit is not None:
            battery["drain_month"] = round(max(-fit[0], 0.0) * 30, 1)
        estimate = self._battery_estimate(bat, now) if bat else None
        if estimate is not None:
            battery["estimate_days"] = estimate
            # The estimate counts from the latest reading, which is where the projection starts.
            battery["empty_at"] = int(bat[-1][0] + estimate * 86400)
        return {"range": range_key, "start": int(start), "end": int(now), "lqi": lqi, "battery": battery}
