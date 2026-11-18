"""Replacing a device that still works, or with another model: removal, renaming the old one, tasks, bindings."""

from __future__ import annotations

from conftest import SENSOR, send, z2m_device

NEW = "0x00158d0000700001"
LIGHT = "0x00158d0000700002"


def endpoints(spec: dict[int, list[str]], bindings: dict[int, list[dict]] | None = None) -> dict:
    return {
        str(ep): {"clusters": {"input": ["genBasic"], "output": clusters}, "bindings": (bindings or {}).get(ep, [])}
        for ep, clusters in spec.items()
    }


def to_light(cluster: str = "genOnOff") -> dict:
    return {"cluster": cluster, "target": {"type": "endpoint", "ieee_address": LIGHT, "endpoint": 1}}


def calls(tracker, monkeypatch, answers):
    """Record bridge requests; answer each with the next entry of answers (the last one repeats)."""
    seen = []

    def fake(base, path, payload, timeout=30.0):
        seen.append((path, payload))
        return answers[min(len(seen), len(answers)) - 1]

    monkeypatch.setattr(tracker, "request", fake)
    return seen


OK = {"status": "ok", "data": {}}
FAIL = {"status": "error", "error": "Device didn't respond"}


def test_kept_old_device_keeps_its_tasks(tracker):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Leak"), z2m_device(NEW, "New leak")])
    tracker.set_maintenance_task(SENSOR, {"title": "Test with water", "every": 6, "unit": "months"})

    assert "maintenance" in tracker.carry_over(SENSOR, NEW, notes=True, battery=False, keep_old=True)
    assert [t["title"] for t in tracker.data["devices"][NEW]["maintenance"]] == ["Test with water"]
    assert [t["title"] for t in tracker.data["devices"][SENSOR]["maintenance"]] == ["Test with water"]


def test_working_device_is_asked_to_leave(tracker, monkeypatch):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Leak")])
    seen = calls(tracker, monkeypatch, [OK])

    result = tracker.remove_from_z2m(SENSOR, leave=True)

    assert result["ok"] and not result["forced"]
    assert [p["force"] for _, p in seen] == [False]


def test_working_device_that_does_not_answer_is_force_removed(tracker, monkeypatch):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Leak")])
    seen = calls(tracker, monkeypatch, [FAIL, OK])

    result = tracker.remove_from_z2m(SENSOR, leave=True)

    assert result["ok"] and result["forced"]
    assert [p["force"] for _, p in seen] == [False, True]


def test_dead_device_is_force_removed_at_once(tracker, monkeypatch):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Leak")])
    seen = calls(tracker, monkeypatch, [OK])

    assert tracker.remove_from_z2m(SENSOR)["ok"]
    assert [p["force"] for _, p in seen] == [True]


def test_old_name_is_free_after_rename(tracker):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Leak"), z2m_device(NEW, "Leak (old)")])
    assert tracker.old_name_for(SENSOR) == "Leak (old 2)"


def two_switches(tracker, old_eps, new_eps, new_model="FK-1"):
    old = z2m_device(SENSOR, "Switch", endpoints=endpoints(old_eps, {ep: [to_light()] for ep in old_eps}))
    new = z2m_device(NEW, "New", endpoints=endpoints(new_eps))
    new["definition"] = {**new["definition"], "model": new_model}
    send(tracker, "bridge/devices", [old, new, z2m_device(LIGHT, "Lamp", "Router")])


def test_same_model_keeps_endpoint_numbers(tracker, monkeypatch):
    two_switches(tracker, {2: ["genOnOff"]}, {1: ["genOnOff"]})
    seen = calls(tracker, monkeypatch, [OK])

    tracker.rebind(SENSOR, NEW)

    assert [p.get("from_endpoint") for _, p in seen] == [2]


def test_other_model_binds_from_the_endpoint_that_has_the_cluster(tracker, monkeypatch):
    two_switches(tracker, {2: ["genOnOff"]}, {1: ["genOnOff"], 3: ["genLevelCtrl"]}, new_model="IKEA-1")
    seen = calls(tracker, monkeypatch, [OK])

    result = tracker.rebind(SENSOR, NEW)

    assert [p.get("from_endpoint") for _, p in seen] == [1]
    assert result["done"] == 1


def test_other_model_skips_a_binding_with_no_clear_endpoint(tracker, monkeypatch):
    # Old gang 3 has no matching endpoint; the new model has the cluster on two endpoints, so guessing could pick the wrong gang.
    two_switches(tracker, {3: ["genOnOff"]}, {1: ["genOnOff"], 2: ["genOnOff"]}, new_model="IKEA-2")
    seen = calls(tracker, monkeypatch, [OK])

    result = tracker.rebind(SENSOR, NEW)

    assert seen == []
    assert result["done"] == 0 and result["unfit"] == 1
