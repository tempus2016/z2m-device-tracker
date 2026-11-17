"""Routing scans: the time limit, late answers and scans left running at shutdown (issue #38)."""

from __future__ import annotations

import json
import logging
import threading

from conftest import send

NETWORKMAP_OK = {"status": "ok", "data": {"type": "raw", "routes": False, "value": {"nodes": [], "links": []}}}


def test_scan_waits_for_the_given_time_limit(tracker):
    waits = []

    def fake_request(topic, path, payload, timeout=30.0):
        waits.append(timeout)
        return NETWORKMAP_OK

    tracker.request = fake_request
    assert tracker.scan_networkmap("zigbee2mqtt", timeout=1800) == {"zigbee2mqtt": "ok"}
    assert waits == [1800]


def test_scan_counts_as_running_until_zigbee2mqtt_answers(tracker):
    published = []
    sent = threading.Event()

    def publish(topic, payload):
        published.append(payload)
        sent.set()

    tracker.publish = publish
    result = {}
    worker = threading.Thread(target=lambda: result.update(tracker.scan_networkmap("zigbee2mqtt", timeout=30)))
    worker.start()
    assert sent.wait(5)
    assert tracker.data["networkmap"]["zigbee2mqtt"].get("running_since")

    send(tracker, "bridge/response/networkmap", {**NETWORKMAP_OK, "transaction": published[0]["transaction"]})
    worker.join(5)
    assert result == {"zigbee2mqtt": "ok"}
    info = tracker.data["networkmap"]["zigbee2mqtt"]
    assert "running_since" not in info
    assert info["status"] == "ok"


def test_late_answer_is_saved_and_logged(tracker, caplog):
    tracker.publish = lambda topic, payload: None
    assert tracker.scan_networkmap("zigbee2mqtt", timeout=0) == {"zigbee2mqtt": "timeout"}
    assert tracker.data["networkmap"]["zigbee2mqtt"]["status"] == "timeout"

    with caplog.at_level(logging.INFO, logger="z2m_tracker"):
        send(tracker, "bridge/response/networkmap", {**NETWORKMAP_OK, "transaction": "z2mt-0123456789"})
    assert tracker.data["networkmap"]["zigbee2mqtt"]["status"] == "ok"
    assert "answered after the time limit" in caplog.text


def test_scan_left_running_at_shutdown_is_cleared_on_load(make_tracker, data_dir):
    stored = {
        "devices": {},
        "events": [],
        "networkmap": {"zigbee2mqtt": {"running_since": "2026-06-01T03:00:00+00:00"}},
    }
    (data_dir / "history.json").write_text(json.dumps(stored))
    tracker = make_tracker()
    assert tracker.load() == "saved"
    assert "running_since" not in tracker.data["networkmap"]["zigbee2mqtt"]
