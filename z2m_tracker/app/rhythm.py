"""Device rhythm: how long a device normally goes between reports, by time of week.

A device's gaps (seconds between two messages) are counted in a table keyed "<slot>:<band>":

- slot: the local week in 56 blocks of 3 hours, Monday 00:00-03:00 = 0, so slot = weekday * 8 + hour // 3.
  The slot is the one the gap started in.
- band: 23 log-spaced upper edges from 30 s to 7 days (BANDS); band 23 is "over 7 days".
- weight: what one gap is worth. A normal gap adds 1, a gap that ended an outage adds 0.5, and a gap
  the user taught ("this is normal") adds 4, enough to count as recurring at once. Every day all weights
  shrink by 2% and entries under 0.05 are dropped, so old habits fade.

limit() turns the table into a number of seconds: the upper edge of the largest band that keeps
coming back in that slot (or at the same time of day on the other days), times 1.5 for margin. A band
is recurring at weight 3. One outage never reaches that. Where the same time of day has fewer than
10 gaps, the whole table is asked instead. Only a steady device (most gaps near its usual one) gets a
limit: one that reports only when something happens has no silence that means anything is wrong.
A device is learned after 7 days and 30 gaps filed.

Pure functions, no I/O: the caller stores the table.
"""

from __future__ import annotations

import math
from datetime import datetime, tzinfo
from typing import Any

SLOTS = 56
MIN_LIMIT, MAX_LIMIT = 900, 604800
LEARN_DAYS, LEARN_GAPS = 7, 30
RECURRING, THIN_SLOT, MARGIN, DECAY, DROP_BELOW = 3.0, 10, 1.5, 0.98, 0.05
STEADY_SHARE = 0.8  # share of the weight within one band of the median band for a device to count as steady
WEIGHT_NORMAL, WEIGHT_OVER, WEIGHT_TAUGHT = 1.0, 0.5, 4.0

_EDGES = 23
_OVER = _EDGES  # band 23: over 7 days


def _make_bands() -> tuple[float, ...]:
    bands = [float(round(30 * (604800 / 30) ** (i / (_EDGES - 1)))) for i in range(_EDGES)]
    bands[0], bands[-1] = 30.0, 604800.0
    return tuple(bands)


BANDS = _make_bands()


def band_of(seconds: float) -> int | None:
    """Band index 0..23 for a gap, or None for a gap that is not a positive finite number."""
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(s) or s <= 0:
        return None
    for i, edge in enumerate(BANDS):
        if s <= edge:
            return i
    return _OVER


def _edge(band: int) -> float:
    return BANDS[min(band, _EDGES - 1)]


def slot_of(ts: float, tz: tzinfo) -> int:
    """Slot 0..55 of a timestamp in the local week."""
    dt = datetime.fromtimestamp(ts, tz)
    return dt.weekday() * 8 + dt.hour // 3


def _parse(key: str) -> tuple[int, int] | None:
    slot, sep, band = key.partition(":")
    if not sep or not (slot.isascii() and slot.isdigit() and band.isascii() and band.isdigit()):
        return None
    s, b = int(slot), int(band)
    return (s, b) if s < SLOTS and b <= _OVER else None


def clean(gaps: Any) -> dict[str, float]:
    """Keep only entries shaped like "<0..55>:<0..23>" with a finite weight above 0."""
    if not isinstance(gaps, dict):
        return {}
    out: dict[str, float] = {}
    for key, weight in gaps.items():
        if not isinstance(key, str) or isinstance(weight, bool) or not isinstance(weight, (int, float)):
            continue
        parsed = _parse(key)
        try:
            weight = float(weight)
        except OverflowError:  # an int too large for a float
            continue
        if parsed is None or not math.isfinite(weight) or weight <= 0:
            continue
        canon = f"{parsed[0]}:{parsed[1]}"
        out[canon] = out.get(canon, 0.0) + float(weight)
    return out


def add(gaps: dict[str, float], slot: int, band: int, weight: float) -> None:
    key = f"{slot}:{band}"
    gaps[key] = gaps.get(key, 0.0) + weight


def decay(gaps: dict[str, float]) -> None:
    """One day's decay, in place."""
    for key in list(gaps):
        gaps[key] *= DECAY
        if gaps[key] < DROP_BELOW:
            del gaps[key]


def total(gaps: dict[str, float]) -> float:
    return sum(gaps.values())


def learned(count: int, since: float | None, now: float) -> bool:
    """7 days since the first message and 30 gaps filed (the undecayed count, not the table's weight)."""
    return since is not None and now - since >= LEARN_DAYS * 86400 and count >= LEARN_GAPS


def _entries(gaps: dict[str, float]) -> list[tuple[int, int, float]]:
    out = []
    for key, weight in gaps.items():
        parsed = _parse(key)
        if parsed is not None:
            out.append((parsed[0], parsed[1], weight))
    return out


def _percentile_band(entries: list[tuple[int, int, float]], fraction: float) -> int:
    per_band: dict[int, float] = {}
    for _, b, w in entries:
        per_band[b] = per_band.get(b, 0.0) + w
    target = sum(per_band.values()) * fraction
    run = 0.0
    for b in sorted(per_band):
        run += per_band[b]
        if run >= target:
            return b
    return max(per_band)


def _steady(entries: list[tuple[int, int, float]]) -> bool:
    if not entries:
        return False
    # Gaps under 30 s (band 0) are one report sent as several messages (temperature, then humidity): left out.
    # A device with nothing else reports faster than every 30 s, which is steady.
    spaced = [e for e in entries if e[1] > 0]
    if not spaced:
        return True
    mid = _percentile_band(spaced, 0.5)
    near = sum(w for _, b, w in spaced if abs(b - mid) <= 1)
    return near >= STEADY_SHARE * sum(w for _, _, w in spaced)


def steady(gaps: dict[str, float]) -> bool:
    """True when at least STEADY_SHARE of the weight lies within one band of the weighted-median band,
    leaving out gaps under 30 s (bursts within one report).

    A device that reports on a timer is steady, with or without a nightly sleep (one long gap a day is a
    small share). A button or a contact sensor without a check-in spreads its gaps over many bands: it
    gets no learned limit, since none of its silences says anything is wrong."""
    return _steady(_entries(gaps))


class _Tables:
    """A table summed three ways: per slot, per time-of-day group (slot % 8) and over the whole week."""

    def __init__(self, entries: list[tuple[int, int, float]]) -> None:
        self.by_slot: dict[int, dict[int, float]] = {}
        self.groups: dict[int, dict[int, float]] = {}
        self.whole: dict[int, float] = {}
        for s, b, w in entries:
            self.by_slot.setdefault(s, {})[b] = w
            group = self.groups.setdefault(s % 8, {})
            group[b] = group.get(b, 0.0) + w
            self.whole[b] = self.whole.get(b, 0.0) + w

    def band(self, slot: int) -> int | None:
        """The largest band recurring in this slot alone or over its time-of-day group; the whole table
        stands in for a group with fewer than THIN_SLOT gaps. limit() and describe() both use it."""
        group = self.groups.get(slot % 8, {})
        pool = self.whole if sum(group.values()) < THIN_SLOT else group
        found = [b for bands in (pool, self.by_slot.get(slot, {})) for b, w in bands.items() if w >= RECURRING]
        return max(found) if found else None


def _limit_of(band: int | None) -> int | None:
    if band is None:
        return None
    return int(min(MAX_LIMIT, max(MIN_LIMIT, _edge(band) * MARGIN)))


def limit(gaps: dict[str, float], slot: int) -> int | None:
    """Seconds of silence that are normal in this slot, or None when the device isn't steady or no band recurs."""
    entries = _entries(gaps)
    return _limit_of(_Tables(entries).band(slot)) if _steady(entries) else None


def describe(
    gaps: dict[str, float], since: float | None, now: float, count: int = 0, slot: int | None = None
) -> dict[str, Any]:
    """What the UI shows. count is the number of gaps filed (Metrics keeps it); with slot, "slot_limit" is
    limit() for that slot, worked out from the same parse, and "slot_longest" the upper edge of the band it
    was built from (before the margin and the clamp)."""
    entries = _entries(gaps)
    days = max(0, int((now - since) // 86400)) if since is not None else 0
    longest = None
    sleep_slots: list[int] = []
    usual = None
    is_steady = _steady(entries)
    slot_limit = slot_longest = None
    if entries:
        # The range spans the 25th percentile band's lower edge to the 75th percentile band's upper edge.
        low = _percentile_band(entries, 0.25)
        usual = [_edge(low - 1) if low > 0 else 0.0, _edge(_percentile_band(entries, 0.75))]
        tables = _Tables(entries)
        found = [b for b in (tables.band(s) for s in range(SLOTS)) if b is not None]
        if found:
            top = max(found)
            longest = _edge(top)
            # Where the long gaps start: slots holding at least one whole gap in that band or the one below.
            sleep_slots = sorted(
                s for s, bands in tables.by_slot.items() if bands.get(top, 0.0) >= 1 or bands.get(top - 1, 0.0) >= 1
            )
        if slot is not None and is_steady:
            band = tables.band(slot)
            slot_limit = _limit_of(band)
            slot_longest = _edge(band) if band is not None else None
    return {
        "learned": learned(count, since, now),
        "steady": is_steady,
        "days": days,
        "gaps": count,
        "need_days": LEARN_DAYS,
        "need_gaps": LEARN_GAPS,
        "usual": usual,
        "longest": longest,
        "sleep_slots": sleep_slots,
        "slot_limit": slot_limit,
        "slot_longest": slot_longest,
    }
