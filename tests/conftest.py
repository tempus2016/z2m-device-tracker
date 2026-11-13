"""Shared fixtures: a fake clock and a Tracker wired to a temporary data directory."""

from __future__ import annotations

import json
import types
from datetime import datetime, timezone
from pathlib import Path

import metrics as metrics_module
import pytest
import tracker as tracker_module
from metrics import Metrics
from tracker import Tracker

FIXTURES = Path(__file__).parent / "fixtures"
START = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp()

COORD = "0x00124b0000c00001"
ROUTER_A = "0x00124b0000a00001"
ROUTER_B = "0x00124b0000b00001"
SENSOR = "0x00158d0000500001"
PLUG = "0x00158d0000600001"


class FakeClock:
    """Stands in for time.time() and the tracker's _now() so tests never sleep."""

    def __init__(self, start: float = START) -> None:
        self.now = start

    def time(self) -> float:
        return self.now

    def iso(self) -> str:
        return datetime.fromtimestamp(self.now, tz=timezone.utc).isoformat(timespec="seconds")

    def advance(self, seconds: float = 0, *, minutes: float = 0, hours: float = 0, days: float = 0) -> None:
        self.now += seconds + minutes * 60 + hours * 3600 + days * 86400


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(tracker_module, "_now", fake.iso)
    monkeypatch.setattr(tracker_module, "time", types.SimpleNamespace(time=fake.time))
    monkeypatch.setattr(metrics_module, "time", types.SimpleNamespace(time=fake.time))
    return fake


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    path = tmp_path / "data"
    path.mkdir()
    return path


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    path = tmp_path / "config"
    path.mkdir()
    return path


@pytest.fixture
def make_tracker(clock: FakeClock, data_dir: Path, config_dir: Path):
    def factory(base_topics: list[str] | None = None, options: dict | None = None) -> Tracker:
        return Tracker(
            str(data_dir / "history.json"),
            base_topics or ["zigbee2mqtt"],
            str(config_dir),
            timezone.utc,
            Metrics(str(data_dir / "metrics.json")),
            options if options is not None else {},
            {},
        )

    return factory


@pytest.fixture
def tracker(make_tracker) -> Tracker:
    return make_tracker()


def send(tracker: Tracker, topic: str, payload, base: str = "zigbee2mqtt") -> None:
    """Deliver an MQTT message the way main.py does."""
    if isinstance(payload, (dict, list)):
        raw = json.dumps(payload).encode()
    else:
        raw = str(payload).encode()
    tracker.handle_message(base, f"{base}/{topic}", raw)


def z2m_device(ieee: str, name: str, dtype: str = "EndDevice", **extra) -> dict:
    """A bridge/devices entry with the fields the tracker reads."""
    device = {
        "ieee_address": ieee,
        "friendly_name": name,
        "type": dtype,
        "interview_state": "SUCCESSFUL",
        "power_source": "Battery" if dtype == "EndDevice" else "Mains (single phase)",
        "model_id": "lumi.fake",
        "manufacturer": "FakeCo",
        "definition": {
            "model": "FK-1",
            "vendor": "FakeCo",
            "description": "Fake sensor",
            "exposes": [{"property": "temperature"}, {"property": "linkquality"}],
        },
    }
    device.update(extra)
    return device


def coordinator(ieee: str = COORD) -> dict:
    return {"ieee_address": ieee, "friendly_name": "Coordinator", "type": "Coordinator", "definition": None}


def events_of(tracker: Tracker, kind: str | None = None) -> list[dict]:
    return [e for e in tracker.data["events"] if kind is None or e["kind"] == kind]
