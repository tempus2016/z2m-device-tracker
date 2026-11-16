"""Time-series for link quality (hourly and daily), battery (daily), router reply time (daily) and command
reply time (hourly), kept in a separate file."""

from __future__ import annotations

import json
import math
import os
import random
import threading
import time
from datetime import datetime, timedelta, tzinfo
from datetime import time as clock_time
from statistics import median
from typing import Any


def _finite_only(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _finite_only(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_finite_only(v) for v in value]
    return value


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
# Commands: time from a <name>/set message to the device's next state message (see Tracker._handle_command).
CMD_KEEP_HOURS = 24 * 14
CMD_SAMPLES_PER_HOUR = 60  # reply times kept per hour; past that a random sample stands in for the rest
CMD_CHART_DAYS = 7
CMD_MIN = 10  # commands in the last 24 h before either check looks at a device
CMD_SLOW_MS = 1000  # slow_commands: typical (median) reply above this...
CMD_SLOW_FACTOR = 3  # ...or this many times its own usual over the 7 days before
CMD_SLOW_FLOOR_MS = 300  # ...and above this, so 40 ms to 130 ms isn't flagged
CMD_BASE_MIN = 20  # answered commands needed in those 7 days for a usual value
CMD_MISSED_SHARE = 0.2  # missed_commands: at least this share of commands got no reply


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


# Network-wide daily signal dips (see Metrics.network_hours).
DIP_DAYS = 7  # complete local days looked at
DIP_MIN_COVERAGE = 0.6  # a device counts if it has readings in this share of those hours
DIP_MIN_DEVICES = 3  # fewer counted devices than this and there's no chart or check
DIP_DROP = 0.25  # an hour dips when the network is this far below that day's median
DIP_MIN_DAYS = 4  # ...on at least this many of the days
DIP_MAX_HOURS = 4  # longer low stretches aren't a dip (something else is going on)
DIP_MIN_SHARE = 0.3  # share of counted devices that must dip too
DIP_DEVICE_DROP = 0.15  # a device dips when its usual level in those hours is this far below its own median


def _real_battery(point: Any) -> bool:
    """A stored [day, value] battery point with a real level. Older versions kept values like 255 %."""
    value = point[1] if isinstance(point, (list, tuple)) and len(point) == 2 else None
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 100


class Metrics:
    """Per-device LQI hourly averages, hourly message counts and daily battery readings."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.lock = threading.RLock()
        # {ieee: {"lqi": [[hour_ts, sum, count], ...], "msg": [[hour_ts, count], ...], "bat": [[day_ts, value], ...],
        #         "lqid": [[day_ts, min, sum, count, max], ...], "ping": [[day_ts, ms or None], ...],
        #         "cmd": [[hour_ts, commands, missed, [reply ms, ...]], ...]}}
        # "ping" holds one nightly reply time per router per day; None means it didn't reply.
        # "cmd" counts commands per hour; the list holds reply times of the answered ones (at most
        # CMD_SAMPLES_PER_HOUR, a random sample beyond that).
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
            try:
                payload = json.dumps(self.series, separators=(",", ":"), allow_nan=False)
            except ValueError:
                # Data saved before readings were checked: store NaN and infinity as no reading.
                payload = json.dumps(_finite_only(self.series), separators=(",", ":"))
            self.dirty = False
        # A unique name and fsync: two saves at once, or a power cut, can't leave a half-written file.
        tmp = f"{self.path}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)

    def forget(self, ieee: str) -> None:
        with self.lock:
            if self.series.pop(ieee, None) is not None:
                self.dirty = True

    # ----------------------------------------------------------------- record

    def add_lqi(self, ieee: str, value: Any, now: float | None = None) -> None:
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
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
        # Outside 0-100 % isn't a real battery level (see odd_readings): never part of an estimate.
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            return None
        if not 0 <= value <= 100:
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
        if ms is not None and (
            not isinstance(ms, (int, float)) or isinstance(ms, bool) or not math.isfinite(ms) or ms < 0
        ):
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

    def add_command(self, ieee: str, ms: float | None, now: float | None = None) -> None:
        """Record one command: its reply time in ms, or None when no state message came back in time."""
        if ms is not None and (
            not isinstance(ms, (int, float)) or isinstance(ms, bool) or not math.isfinite(ms) or ms < 0
        ):
            return
        now = now or time.time()
        hour = int(now // 3600 * 3600)
        with self.lock:
            points = self.series.setdefault(ieee, {}).setdefault("cmd", [])
            if points and points[-1][0] == hour:
                entry = points[-1]
            else:
                entry = [hour, 0, 0, []]
                points.append(entry)
                cutoff = hour - CMD_KEEP_HOURS * 3600
                while points and points[0][0] < cutoff:
                    points.pop(0)
            entry[1] += 1
            if ms is None:
                entry[2] += 1
            else:
                value = round(float(ms))
                samples = entry[3]
                if len(samples) < CMD_SAMPLES_PER_HOUR:
                    samples.append(value)
                else:
                    # Reservoir sampling: every answered command this hour has the same chance to be kept.
                    slot = random.randrange(entry[1] - entry[2])
                    if slot < CMD_SAMPLES_PER_HOUR:
                        samples[slot] = value
            self.dirty = True

    @staticmethod
    def command_stats(points: list[list], now: float) -> dict[str, Any]:
        """Command counts and reply times: last 24 h, the 7 days before as the usual, and a daily chart.

        Also the two checks: "slow" (typical reply over CMD_SLOW_MS, or CMD_SLOW_FACTOR times its usual
        and over CMD_SLOW_FLOOR_MS) and "missed_share" (CMD_MISSED_SHARE or more without a reply), each
        only with at least CMD_MIN commands in the last 24 h.
        """
        hour_now = int(now // 3600 * 3600)
        recent = [p for p in points if p[0] > hour_now - 24 * 3600]
        base = [p for p in points if hour_now - 8 * 86400 < p[0] <= hour_now - 24 * 3600]

        def pick(rows: list[list]) -> tuple[int, int, list[float]]:
            samples = sorted(ms for row in rows for ms in row[3] if _finite(ms))
            return sum(row[1] for row in rows), sum(row[2] for row in rows), samples

        commands, missed, samples = pick(recent)
        _, _, base_samples = pick(base)
        out: dict[str, Any] = {
            "commands_24h": commands,
            "missed_24h": missed,
            "median_ms": round(_median(samples)) if samples else None,
            "p90_ms": round(samples[math.ceil(0.9 * len(samples)) - 1]) if samples else None,
            "usual_ms": round(_median(base_samples)) if len(base_samples) >= CMD_BASE_MIN else None,
        }
        today = int(now // 86400 * 86400)
        days: dict[int, list] = {}
        for row in points:
            day = int(row[0] // 86400 * 86400)
            if day > today - CMD_CHART_DAYS * 86400:
                entry = days.setdefault(day, [0, 0, []])
                entry[0] += row[1]
                entry[1] += row[2]
                entry[2].extend(ms for ms in row[3] if _finite(ms))
        # [day_ts, typical ms (None when every command was missed), commands, missed]
        out["daily"] = [[day, round(_median(v[2])) if v[2] else None, v[0], v[1]] for day, v in sorted(days.items())]

        median_ms, usual_ms = out["median_ms"], out["usual_ms"]
        answered = len(samples)
        if commands >= CMD_MIN and answered >= CMD_MIN and median_ms is not None:
            if median_ms > CMD_SLOW_MS:
                out["slow"] = {"median_ms": median_ms, "usual_ms": usual_ms, "n": commands}
            elif usual_ms and median_ms > CMD_SLOW_FLOOR_MS and median_ms >= CMD_SLOW_FACTOR * usual_ms:
                out["slow"] = {"median_ms": median_ms, "usual_ms": usual_ms, "n": commands, "rise": True}
        if commands >= CMD_MIN and missed / commands >= CMD_MISSED_SHARE:
            out["missed_share"] = {"missed": missed, "n": commands, "percent": round(missed / commands * 100)}
        return out

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
            bat = [list(p) for p in data.get("bat", []) if _real_battery(p)]
            ping = [list(p) for p in data.get("ping", [])]
            # Restored backups can hold anything: skip rows that aren't [hour, commands, missed, [ms, ...]].
            cmd = [
                [p[0], p[1], p[2], list(p[3])]
                for p in data.get("cmd", [])
                if isinstance(p, list) and len(p) == 4 and all(_finite(x) for x in p[:3]) and isinstance(p[3], list)
            ]
        out: dict[str, Any] = {}

        if cmd:
            out["cmd"] = self.command_stats(cmd, now)

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
            out["lqi_7d"] = avg_since(7 * 86400)
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

    # --------------------------------------------------------- network hours

    def network_hours(self, ieees: list[str], local_tz: tzinfo, now: float | None = None) -> dict[str, Any] | None:
        """Average link quality by local hour of day over the last 7 complete days, plus daily dips.

        Each device is divided by its own 7-day mean first, so a network average isn't pulled around by
        which devices happen to report in a given hour. The per-hour value shown is that relative level
        times the devices' average mean, so it reads as LQI. None when there isn't enough data.
        """
        now = now or time.time()
        today = datetime.fromtimestamp(now, local_tz).date()
        first = today - timedelta(days=DIP_DAYS)
        start = datetime.combine(first, clock_time(), tzinfo=local_tz).timestamp()
        end = datetime.combine(today, clock_time(), tzinfo=local_tz).timestamp()
        slot_of: dict[int, tuple[int, int]] = {}
        for hour in range(int(start // 3600 * 3600), int(end), 3600):
            if hour >= start:
                local = datetime.fromtimestamp(hour, local_tz)
                slot_of[hour] = ((local.date() - first).days, local.hour)
        slots = len(set(slot_of.values()))
        with self.lock:
            raw = {ieee: [p for p in (self.series.get(ieee) or {}).get("lqi", []) if p[0] in slot_of] for ieee in ieees}

        devices: list[dict[tuple[int, int], float]] = []
        means: list[float] = []
        for points in raw.values():
            cells: dict[tuple[int, int], list[float]] = {}
            for hour, value_sum, n in points:
                if n:
                    cell = cells.setdefault(slot_of[hour], [0.0, 0])
                    cell[0] += value_sum
                    cell[1] += n
            if not slots or len(cells) < DIP_MIN_COVERAGE * slots:
                continue
            avgs = {slot: total / n for slot, (total, n) in cells.items()}
            mean = sum(avgs.values()) / len(avgs)
            if mean < 5:
                continue
            means.append(mean)
            devices.append({slot: value / mean for slot, value in avgs.items()})
        if len(devices) < DIP_MIN_DEVICES:
            return None

        # Network level per (day, hour): mean of the relative levels of devices heard in that hour.
        enough = max(2, len(devices) * 0.3)
        net: dict[tuple[int, int], float] = {}
        for slot in set(slot_of.values()):
            values = [d[slot] for d in devices if slot in d]
            if len(values) >= enough:
                net[slot] = sum(values) / len(values)

        dipped = [0] * 24
        days = 0
        for day in range(DIP_DAYS):
            row = {h: net[(day, h)] for h in range(24) if (day, h) in net}
            if len(row) < 12:
                continue
            days += 1
            mid = median(row.values())
            for h, value in row.items():
                if value <= (1 - DIP_DROP) * mid:
                    dipped[h] += 1

        profile: list[float | None] = []
        for h in range(24):
            values = [net[(day, h)] for day in range(DIP_DAYS) if (day, h) in net]
            profile.append(sum(values) / len(values) if values else None)
        known = [v for v in profile if v is not None]
        if days < DIP_MIN_DAYS or len(known) < 12:
            return None
        usual = median(known)
        baseline = sum(means) / len(means)

        # Each device's own level by hour of day, for counting how many devices share a dip.
        device_hours = []
        for cells in devices:
            by_hour: dict[int, list[float]] = {}
            for (_, h), value in cells.items():
                by_hour.setdefault(h, []).append(value)
            levels = {h: sum(v) / len(v) for h, v in by_hour.items()}
            device_hours.append((levels, median(levels.values())))

        dips = []
        for run in _runs([count >= DIP_MIN_DAYS for count in dipped]):
            if len(run) > DIP_MAX_HOURS:
                continue
            affected = 0
            for levels, own in device_hours:
                inside = [levels[h] for h in run if h in levels]
                if inside and sum(inside) / len(inside) <= (1 - DIP_DEVICE_DROP) * own:
                    affected += 1
            if affected < DIP_MIN_SHARE * len(devices):
                continue
            inside = [profile[h] for h in run if profile[h] is not None]
            drop = 1 - (sum(inside) / len(inside)) / usual if inside else 0
            dips.append(
                {
                    "start": run[0],
                    "hours": len(run),
                    "devices": affected,
                    "days": min(dipped[h] for h in run),
                    "drop": round(drop * 100),
                }
            )
        dips.sort(key=lambda d: (-d["drop"], d["start"]))
        return {
            "devices": len(devices),
            "days": days,
            "hours": [
                None if v is None else {"lqi": round(v * baseline, 1), "drop": round((1 - v / usual) * 100)}
                for v in profile
            ],
            "dips": dips,
        }

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
            bat = [list(p) for p in data.get("bat", []) if _real_battery(p)]

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


def _runs(flags: list[bool]) -> list[list[int]]:
    """Contiguous runs of True hours on a 24-hour clock, so a run can wrap past midnight."""
    if all(flags) or not any(flags):
        return []
    begin = flags.index(False)
    runs: list[list[int]] = []
    current: list[int] = []
    for step in range(1, len(flags) + 1):
        h = (begin + step) % len(flags)
        if flags[h]:
            current.append(h)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    return runs
