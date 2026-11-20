"""What the integration puts into Home Assistant's Repairs (custom_components/z2m_tracker/const.py, pure Python)."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest
import yaml


@pytest.fixture(scope="module")
def rules():
    """Load the integration's const.py without importing Home Assistant."""
    root = Path(__file__).parent.parent / "custom_components" / "z2m_tracker"
    pkg = types.ModuleType("z2mt_rep")
    pkg.__path__ = [str(root)]
    sys.modules["z2mt_rep"] = pkg
    spec = importlib.util.spec_from_file_location("z2mt_rep.const", root / "const.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["z2mt_rep.const"] = module
    spec.loader.exec_module(module)
    yield module
    for name in ("z2mt_rep", "z2mt_rep.const"):
        sys.modules.pop(name, None)


def issue(key: str, kind: str, severity: str = "warning", **extra) -> dict:
    return {"key": key, "kind": kind, "severity": severity, "ieee": key.split(":", 1)[1], **extra}


def test_serious_kinds(rules):
    assert rules.serious_repair(issue("offline:0xa", "offline", critical=True)) == "critical_offline"
    assert rules.serious_repair(issue("offline:0xa", "offline")) is None
    assert rules.serious_repair(issue("network_offline:z", "network_offline")) == "network_offline"
    assert rules.serious_repair(issue("network_offline:z", "network_offline", severity="info", expected=True)) is None
    assert rules.serious_repair(issue("coordinator_backup:z", "coordinator_backup")) == "coordinator_backup"
    assert rules.serious_repair(issue("battery_low:0xa", "battery_low")) is None


def test_switches(rules):
    assert rules.repairs_on({})
    assert not rules.repairs_on({"ha_repairs": False})
    assert not rules.repairs_on({"alerts_enabled": False})
    assert rules.repairs_on({"ha_repairs": True, "alerts_enabled": True})


def test_serious_problems_survive_a_flood(rules):
    flood = [issue(f"weak_signal:0x{n:04x}", "weak_signal") for n in range(60)]
    serious = [
        issue("offline:0xcrit", "offline", critical=True),
        issue("network_offline:zigbee2mqtt", "network_offline"),
        issue("coordinator_backup:zigbee2mqtt", "coordinator_backup", severity="info"),
    ]
    wanted = rules.repairs_wanted(flood + serious, {}, 50)
    assert len(wanted) == 50
    assert {"issue_offline_0xcrit", "issue_network_offline_zigbee2mqtt", "issue_coordinator_backup_zigbee2mqtt"} <= set(
        wanted
    )


def test_info_issues_need_notify_and_nested_ones_are_skipped(rules):
    issues = [
        issue("chatty:0xa", "chatty", severity="info"),
        issue("late:0xb", "late", severity="info"),
        issue("offline:0xr", "offline"),
        issue("offline:0xc", "offline", behind="0xr"),
    ]
    wanted = rules.repairs_wanted(issues, {"notify_checks": ["late"]}, 50)
    assert sorted(wanted) == ["issue_late_0xb", "issue_offline_0xr"]


def test_app_option_is_declared():
    config = yaml.safe_load((Path(__file__).parent.parent / "z2m_tracker" / "config.yaml").read_text())
    assert config["options"]["ha_repairs"] is True
    assert config["schema"]["ha_repairs"] == "bool?"
