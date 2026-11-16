"""Mesh score: the formula, the daily history, the tracker's inputs, discovery and Prometheus."""

from __future__ import annotations

import json
import types
from datetime import date

import prometheus
from conftest import COORD, PLUG, ROUTER_A, ROUTER_B, SENSOR
from discovery import Discovery
from mesh_score import HISTORY_DAYS, clean, record, score, summary
from tracker import STATUS_ACTIVE


def test_score_all_good():
    s = score(online=10, total=10, lqi=150, backup=10, backup_of=10, problems=0)
    assert s["score"] == 100
    assert s["parts"] == {"online": 1.0, "lqi": 1.0, "backup": 1.0, "problems": 1.0}


def test_score_weights():
    # Online 40 * 0.5 + LQI 20 * 0.5 + backup 20 * 0.5 + problems 20 * (1 - 2/5)
    s = score(online=5, total=10, lqi=50, backup=4, backup_of=8, problems=2)
    assert s["score"] == round(20 + 10 + 10 + 12)


def test_missing_parts_are_left_out():
    s = score(online=4, total=4, lqi=None, backup=None, backup_of=None, problems=0)
    assert s["score"] == 100 and set(s["parts"]) == {"online", "problems"}
    assert (s["lqi"], s["backup"], s["backup_of"]) == (None, None, None)
    assert score(online=0, total=0, lqi=None, backup=None, backup_of=None, problems=0) is None


def test_problems_scale_with_network_size():
    small = score(online=10, total=10, lqi=None, backup=None, backup_of=None, problems=5)
    big = score(online=100, total=100, lqi=None, backup=None, backup_of=None, problems=5)
    assert small["parts"]["problems"] == 0 and big["parts"]["problems"] == 0.75


def test_record_keeps_one_entry_a_day_and_trims():
    rows: list = []
    cur = score(online=1, total=1, lqi=None, backup=None, backup_of=None, problems=0)
    assert record(rows, date(2026, 6, 1), cur)
    assert not record(rows, date(2026, 6, 1), cur)
    assert record(rows, date(2026, 6, 1), {**cur, "score": 90})
    assert len(rows) == 1 and rows[0][1] == 90
    record(rows, date(2026, 6, 1 + 1), cur)
    record(rows, date(2026, 9, 30), cur)
    # 90 days before 30 Sep is 2 Jul: both June entries go.
    assert [r[0] for r in rows] == ["2026-09-30"]
    assert HISTORY_DAYS == 90


def test_summary_week_and_trend():
    cur = score(online=9, total=10, lqi=90, backup=None, backup_of=None, problems=1)
    rows = [["2026-05-20", 70, 8, 10, 80, None, None, 3], ["2026-05-25", 80, 9, 10, 85, None, None, 2]]
    out = summary(rows, date(2026, 6, 1), cur)
    assert out["week"]["score"] == 80 and out["week"]["online"] == 9
    assert out["trend"] == [["2026-05-20", 70], ["2026-05-25", 80]]
    assert summary([], date(2026, 6, 1), cur)["week"] is None


def test_clean_drops_malformed():
    raw = {"zigbee2mqtt": [["2026-06-01", 90, 1, 1, None, None, None, 0], ["bad"], "x"], "other": "nope"}
    assert clean(raw) == {"zigbee2mqtt": [["2026-06-01", 90, 1, 1, None, None, None, 0]]}
    assert clean(None) == {}


def add(tracker, ieee, kind, availability="online"):
    rec = tracker.data["devices"].setdefault(ieee, {"ieee": ieee})
    rec.update(
        {
            "ieee": ieee,
            "instance": "zigbee2mqtt",
            "status": STATUS_ACTIVE,
            "type": kind,
            "friendly_name": ieee[-4:],
            "availability": availability,
        }
    )


def test_tracker_scores_each_network_and_keeps_history(make_tracker, clock):
    tracker = make_tracker()
    tracker.load()
    for ieee, kind in ((COORD, "Coordinator"), (ROUTER_A, "Router"), (PLUG, "Router"), (SENSOR, "EndDevice")):
        add(tracker, ieee, kind)
    tracker.data["devices"][SENSOR]["availability"] = "offline"
    tracker.evaluate_health()
    m = tracker.mesh["zigbee2mqtt"]
    assert (m["online"], m["total"]) == (2, 3)
    assert m["score"] < 100
    assert tracker.data["mesh_scores"]["zigbee2mqtt"][-1][1] == m["score"]
    snap = json.loads(tracker.snapshot_json())
    assert snap["mesh_scores"]["zigbee2mqtt"]["score"] == m["score"]
    assert tracker.prometheus_snapshot()["mesh_scores"] == {"zigbee2mqtt": m["score"]}
    # Kept across a restart.
    tracker.save(force=True)
    again = make_tracker()
    again.load()
    assert again.data["mesh_scores"]["zigbee2mqtt"][-1][1] == m["score"]


def test_discovery_publishes_a_sensor_per_network():
    mesh = score(online=9, total=10, lqi=90, backup=8, backup_of=9, problems=1)
    disc = Discovery(types.SimpleNamespace(), "1.6.0", True)
    configs, topic = disc._network("zigbee2mqtt")
    (cfg,) = configs.values()
    assert cfg["device"]["name"] == "zigbee2mqtt" and cfg["name"] == "Mesh score"
    assert cfg["unique_id"] == "z2m_tracker_network_zigbee2mqtt_mesh_score"
    assert topic == "z2m_tracker/network/zigbee2mqtt"
    state = disc._network_state({**mesh, "week": {"score": 95}})
    assert state["score"] == mesh["score"] and state["parts"]["score_a_week_ago"] == 95


def test_prometheus_mesh_score():
    text = prometheus.render(prometheus.collect({"mesh_scores": {"zigbee2mqtt": 82}}, "1.6.0"))
    assert 'z2m_tracker_network_mesh_score{network="zigbee2mqtt"} 82' in text


def test_backup_route_part_from_the_routing_scan(tracker, clock):
    from test_tracker_network import CHAIN, link, mesh

    # Hall plug is the only way to Shed plug, and Shed sensor's parent is Shed plug.
    mesh(tracker, CHAIN)
    tracker.evaluate_health()
    m = tracker.mesh["zigbee2mqtt"]
    assert (m["backup"], m["backup_of"]) == (1, 3)
    # A second path for Shed plug gives every device a backup route.
    clock.advance(minutes=5)
    mesh(tracker, [*CHAIN, link(ROUTER_B, COORD, 40)])
    tracker.evaluate_health()
    assert (tracker.mesh["zigbee2mqtt"]["backup"], tracker.mesh["zigbee2mqtt"]["backup_of"]) == (3, 3)
