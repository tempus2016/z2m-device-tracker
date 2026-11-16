"""Reporting settings: slim copy from bridge/devices, the three rules, the reporting_config check and the fix."""

from __future__ import annotations

import health
import pytest
import reporting
from conftest import PLUG, SENSOR, coordinator, events_of, send, z2m_device

TEMP = "msTemperatureMeasurement"
POWER = "haElectricalMeasurement"


def rep(cluster, attribute, low, high, change):
    return {
        "cluster": cluster,
        "attribute": attribute,
        "minimum_report_interval": low,
        "maximum_report_interval": high,
        "reportable_change": change,
    }


def endpoints(*reportings, inputs=(), ep="1"):
    return {
        ep: {
            "bindings": [],
            "configured_reportings": list(reportings),
            "clusters": {"input": ["genBasic", *inputs], "output": []},
            "scenes": [],
        }
    }


def sensor(*reportings, inputs=(TEMP,), **extra):
    return z2m_device(SENSOR, "Hall sensor", endpoints=endpoints(*reportings, inputs=inputs), **extra)


def plug(*reportings, inputs=(POWER,)):
    return z2m_device(
        PLUG,
        "Kettle plug",
        "Router",
        endpoints=endpoints(*reportings, inputs=inputs),
        definition={"model": "P1", "vendor": "FakeCo", "exposes": [{"property": "power"}, {"property": "state"}]},
    )


def audit_of(tracker, ieee):
    return tracker.reporting_audit(tracker.data["devices"][ieee], None)


def kinds(tracker):
    return [(i["kind"], i["ieee"]) for i in tracker.evaluate_health()[0] if i["kind"] == "reporting_config"]


# ------------------------------------------------------------------ slim copy


def test_slim_copy_keeps_only_what_the_rules_need(tracker):
    send(tracker, "bridge/devices", [coordinator(), sensor(rep(TEMP, "measuredValue", 10, 3600, 10))])
    rec = tracker.data["devices"][SENSOR]
    assert rec["reportings"] == [
        {"ep": "1", "cluster": TEMP, "attribute": "measuredValue", "min": 10, "max": 3600, "change": 10}
    ]
    # genBasic isn't a measurement cluster, so it isn't kept.
    assert rec["reporting_inputs"] == {"1": [TEMP]}


def test_no_endpoint_data_keeps_reportings_unknown(tracker):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Hall sensor")])
    assert tracker.data["devices"][SENSOR]["reportings"] is None
    assert audit_of(tracker, SENSOR) is None
    assert kinds(tracker) == []


def test_bad_values_are_dropped_and_the_list_is_capped(tracker):
    bad = [rep(TEMP, "a", "10", 1e300, -1), {"cluster": None}, "junk", rep(TEMP, "b", True, 70000, float("nan"))]
    items, _ = reporting.slim({"endpoints": endpoints(*bad)})
    assert items == [
        {"ep": "1", "cluster": TEMP, "attribute": "a", "min": None, "max": None, "change": None},
        {"ep": "1", "cluster": TEMP, "attribute": "b", "min": None, "max": None, "change": None},
    ]
    many = [rep(TEMP, f"attr{i:02}", 10, 3600, 1) for i in range(60)]
    send(tracker, "bridge/devices", [sensor(*many)])
    assert len(tracker.data["devices"][SENSOR]["reportings"]) == reporting.MAX_REPORTINGS


def test_restored_reportings_are_cleaned(tracker):
    send(tracker, "bridge/devices", [sensor(rep(TEMP, "measuredValue", 10, 3600, 10))])
    rec = tracker.data["devices"][SENSOR]
    rec["reportings"] = [{"ep": 1, "cluster": TEMP, "attribute": "measuredValue", "min": "x"}, 5]
    rec["reporting_inputs"] = "bad"
    cleaned = tracker_module_clean(rec)
    assert cleaned["reportings"] == [
        {"ep": "1", "cluster": TEMP, "attribute": "measuredValue", "min": None, "max": None, "change": None}
    ]
    assert cleaned["reporting_inputs"] == {}


def tracker_module_clean(record):
    import tracker as tracker_module

    return tracker_module._clean_record(record)


def test_interview_not_finished_is_skipped(tracker):
    device = sensor(rep(TEMP, "measuredValue", 0, 60, 0), interview_state="IN_PROGRESS")
    send(tracker, "bridge/devices", [device])
    assert tracker.data["devices"][SENSOR]["reportings"] is None


# ------------------------------------------------------------------ rules


def test_good_battery_sensor_has_no_findings(tracker):
    send(tracker, "bridge/devices", [sensor(rep(TEMP, "measuredValue", 30, 3600, 20))])
    result = audit_of(tracker, SENSOR)
    assert [r["rule"] for r in result["items"]] == [None]
    assert result["missing"] == []
    assert kinds(tracker) == []


def test_battery_timer_under_five_minutes_drains(tracker):
    send(tracker, "bridge/devices", [sensor(rep(TEMP, "measuredValue", 10, 60, 10))])
    item = audit_of(tracker, SENSOR)["items"][0]
    assert item["rule"] == "battery_drain"
    assert item["label"] == "Temperature"
    # Only the maximum was wrong: minimum and change stay.
    assert item["recommended"] == {"min": 10, "max": 3600, "change": 10}


def test_battery_minimum_zero_on_a_measurement_drains(tracker):
    send(tracker, "bridge/devices", [sensor(rep(TEMP, "measuredValue", 0, 3600, 0))])
    item = audit_of(tracker, SENSOR)["items"][0]
    assert item["rule"] == "battery_drain"
    assert item["recommended"] == {"min": 30, "max": 3600, "change": 20}


def test_battery_percentage_min_zero_is_fine_it_changes_slowly(tracker):
    send(tracker, "bridge/devices", [sensor(rep("genPowerCfg", "batteryPercentageRemaining", 0, 43200, 0))])
    assert audit_of(tracker, SENSOR)["items"][0]["rule"] is None


def test_mains_power_every_few_seconds_floods(tracker):
    send(tracker, "bridge/devices", [plug(rep(POWER, "activePower", 1, 5, 1))])
    item = audit_of(tracker, PLUG)["items"][0]
    assert item["rule"] == "mesh_flood"
    assert item["recommended"] == {"min": 10, "max": 3600, "change": 1}


def test_mains_power_on_every_change_floods_unless_throttled(tracker):
    send(tracker, "bridge/devices", [plug(rep(POWER, "activePower", 1, 600, 0))])
    item = audit_of(tracker, PLUG)["items"][0]
    assert item["rule"] == "mesh_flood"
    assert item["recommended"] == {"min": 10, "max": 600, "change": 5}
    send(tracker, "bridge/devices", [plug(rep(POWER, "activePower", 60, 600, 0))])
    assert audit_of(tracker, PLUG)["items"][0]["rule"] is None


def test_mains_temperature_every_few_seconds_is_not_a_flood(tracker):
    send(tracker, "bridge/devices", [plug(rep(TEMP, "measuredValue", 1, 5, 0))])
    assert audit_of(tracker, PLUG)["items"][0]["rule"] is None


def test_max_65535_never_reports(tracker):
    send(tracker, "bridge/devices", [plug(rep(POWER, "activePower", 10, 65535, 5))])
    item = audit_of(tracker, PLUG)["items"][0]
    assert item["rule"] == "never"
    assert item["recommended"] == {"min": 10, "max": 3600, "change": 5}


def test_on_off_with_max_65535_is_not_a_measurement(tracker):
    send(tracker, "bridge/devices", [plug(rep("genOnOff", "onOff", 0, 65535, 0), rep(POWER, "activePower", 10, 600, 5))])
    assert [r["rule"] for r in audit_of(tracker, PLUG)["items"]] == [None, None]


def test_exposed_measurement_without_reporting_is_missing(tracker):
    send(tracker, "bridge/devices", [plug(rep("genOnOff", "onOff", 0, 3600, 0))])
    result = audit_of(tracker, PLUG)
    assert result["missing"] == [
        {"ep": "1", "cluster": POWER, "attribute": "activePower", "key": "power", "label": "Power", "rule": "missing"}
    ]
    assert kinds(tracker) == [("reporting_config", PLUG)]


def test_missing_needs_the_cluster_and_some_configured_reporting(tracker):
    # Without the cluster on any endpoint (the value comes some other way) nothing is missing.
    send(tracker, "bridge/devices", [plug(rep("genOnOff", "onOff", 0, 3600, 0), inputs=())])
    assert audit_of(tracker, PLUG)["missing"] == []
    # A device that uses no configured reporting at all (Aqara, Tuya) isn't flagged either.
    send(tracker, "bridge/devices", [plug()])
    assert audit_of(tracker, PLUG)["missing"] == []


def test_message_rate_backs_up_a_finding(tracker):
    record_rate = {"msg_rate_6h": 900.0}
    send(tracker, "bridge/devices", [plug(rep(POWER, "activePower", 1, 2, 1))])
    record = tracker.data["devices"][PLUG]
    issues = health.device_issues(record, {**record_rate, "battery_device": False}, {}, 0, [])
    issue = next(i for i in issues if i["kind"] == "reporting_config")
    assert issue["msg"] == "reporting_config_rate"
    assert issue["params"] == {"n": 1, "names": "Power (floods the mesh)", "rate": 900}
    assert "about 900 messages an hour" in issue["detail"]
    quiet = health.device_issues(record, {"msg_rate_6h": 30.0, "battery_device": False}, {}, 0, [])
    assert next(i for i in quiet if i["kind"] == "reporting_config")["msg"] == "reporting_config"


# ------------------------------------------------------------------ the check


def test_reporting_config_is_show_only_and_low_severity(tracker):
    send(tracker, "bridge/devices", [sensor(rep(TEMP, "measuredValue", 0, 60, 0))])
    issue = next(i for i in tracker.evaluate_health()[0] if i["kind"] == "reporting_config")
    assert issue["severity"] == "info"
    assert issue["label"] == "Reporting settings"
    assert "reporting_config" not in health.ALERT_KINDS
    assert "reporting_config" not in health.DEFAULT_NOTIFY
    assert "reporting_config" in {key for key, *_ in health.CHECKS}


def test_reporting_config_can_be_turned_off_or_ignored(tracker):
    send(tracker, "bridge/devices", [sensor(rep(TEMP, "measuredValue", 0, 60, 0))])
    tracker.options["disabled_checks"] = ["reporting_config"]
    assert kinds(tracker) == []
    tracker.options["disabled_checks"] = []
    tracker.set_muted(SENSOR, "reporting_config", True)
    assert kinds(tracker) == []


def test_snapshot_carries_the_audit(tracker):
    import json

    send(tracker, "bridge/devices", [sensor(rep(TEMP, "measuredValue", 0, 60, 0))])
    device = next(d for d in json.loads(tracker.snapshot_json())["devices"] if d["ieee"] == SENSOR)
    assert device["reporting_audit"]["battery"] is True
    assert device["reporting_audit"]["items"][0]["rule"] == "battery_drain"


# ------------------------------------------------------------------ plain words


def test_describe_in_plain_words():
    item = {"cluster": TEMP, "attribute": "measuredValue", "min": 10, "max": 3600, "change": 10}
    assert reporting.describe(item) == "Temperature: at least every 10 s, at most every 1 h, on a 0.1 °C change"
    never = {"cluster": POWER, "attribute": "activePower", "min": 0, "max": 65535, "change": 0}
    assert reporting.describe(never) == "Power: as often as it changes, never on a timer, on any change"


# ------------------------------------------------------------------ fix request


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ({"cluster": TEMP, "attribute": "measuredValue", "min": 1, "max": 2, "change": 1}, "required"),
        ({"ep": "1", "cluster": TEMP, "attribute": "measuredValue", "min": -1, "max": 2, "change": 1}, "Minimum"),
        ({"ep": "1", "cluster": TEMP, "attribute": "measuredValue", "min": 1, "max": 65536, "change": 1}, "Maximum"),
        ({"ep": "1", "cluster": TEMP, "attribute": "measuredValue", "min": 1.5, "max": 2, "change": 1}, "Minimum"),
        ({"ep": "1", "cluster": TEMP, "attribute": "measuredValue", "min": True, "max": 2, "change": 1}, "Minimum"),
        ({"ep": "1", "cluster": TEMP, "attribute": "measuredValue", "min": 1, "max": 2, "change": "1"}, "change"),
        ({"ep": "1", "cluster": TEMP, "attribute": "measuredValue", "min": 1, "max": 2, "change": 70000}, "change"),
        ({"ep": "1", "cluster": TEMP, "attribute": "measuredValue", "min": 99, "max": 60, "change": 1}, "longer"),
    ],
)
def test_clean_fix_rejects_bad_input(body, error):
    fix, message = reporting.clean_fix(body)
    assert fix is None
    assert error in message


def test_clean_fix_accepts_good_input():
    body = {"ep": 1, "cluster": TEMP, "attribute": "measuredValue", "min": 30, "max": 3600.0, "change": 20}
    fix, error = reporting.clean_fix(body)
    assert error is None
    assert fix == {"ep": "1", "cluster": TEMP, "attribute": "measuredValue", "min": 30, "max": 3600, "change": 20}


@pytest.fixture
def z2m(tracker):
    """A fake Zigbee2MQTT that answers configure_reporting with ok (or the error in z2m.error)."""

    class Fake:
        published: list = []
        error: str | None = None

    fake = Fake()

    def publish(topic, payload):
        fake.published.append((topic, payload))
        response = {"status": "error", "error": fake.error} if fake.error else {"status": "ok", "data": {}}
        send(
            tracker,
            "bridge/response/device/configure_reporting",
            {**response, "transaction": payload["transaction"]},
        )

    tracker.publish = publish
    return fake


def fix_body(**over):
    return {"ep": "1", "cluster": TEMP, "attribute": "measuredValue", "min": 30, "max": 3600, "change": 20, **over}


def test_fix_sends_configure_reporting_and_logs_it(tracker, z2m):
    send(tracker, "bridge/devices", [sensor(rep(TEMP, "measuredValue", 0, 60, 0))])
    result = tracker.configure_reporting(SENSOR, reporting.clean_fix(fix_body())[0])
    assert result["ok"]
    topic, payload = z2m.published[0]
    assert topic == "zigbee2mqtt/bridge/request/device/configure_reporting"
    assert {k: v for k, v in payload.items() if k != "transaction"} == {
        "id": SENSOR,
        "endpoint": 1,
        "cluster": TEMP,
        "attribute": "measuredValue",
        "minimum_report_interval": 30,
        "maximum_report_interval": 3600,
        "reportable_change": 20,
    }
    assert [e["detail"] for e in events_of(tracker, "reporting changed")] == [
        "Temperature: at least every 30 s, at most every 1 h, on a 0.2 °C change"
    ]
    assert tracker.data["devices"][SENSOR]["reportings"][0]["max"] == 3600
    assert kinds(tracker) == []


def test_fix_failure_is_logged_and_nothing_changes(tracker, z2m):
    send(tracker, "bridge/devices", [sensor(rep(TEMP, "measuredValue", 0, 60, 0))])
    z2m.error = "Failed to configure reporting (timeout)"
    result = tracker.configure_reporting(SENSOR, reporting.clean_fix(fix_body())[0])
    assert not result["ok"] and result["reason"] == "z2m"
    assert len(events_of(tracker, "reporting change failed")) == 1
    assert tracker.data["devices"][SENSOR]["reportings"][0]["max"] == 60


def test_fix_only_for_known_active_devices_and_listed_reportings(tracker, z2m):
    send(tracker, "bridge/devices", [coordinator(), sensor(rep(TEMP, "measuredValue", 0, 60, 0)), plug()])
    fix = reporting.clean_fix(fix_body())[0]
    assert tracker.configure_reporting("0x0000000000000bad", fix)["reason"] == "unknown"
    other = reporting.clean_fix(fix_body(attribute="tolerance"))[0]
    assert tracker.configure_reporting(SENSOR, other)["reason"] == "invalid"
    wrong_ep = reporting.clean_fix(fix_body(ep="2"))[0]
    assert tracker.configure_reporting(SENSOR, wrong_ep)["reason"] == "invalid"
    send(tracker, "bridge/devices", [coordinator(), plug()])
    assert tracker.data["devices"][SENSOR]["status"] == "removed"
    assert tracker.configure_reporting(SENSOR, fix)["reason"] == "invalid"
    assert z2m.published == []
