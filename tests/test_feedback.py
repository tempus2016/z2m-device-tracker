"""Settings > Help & feedback: the pre-filled GitHub issue link and what it leaves out."""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import feedback
import yaml

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / ".github" / "ISSUE_TEMPLATE"

ENV = {
    "app_version": "2.4.0",
    "mode": "Home Assistant app",
    "ha_version": "2026.11.2",
    "install": "Home Assistant OS 16.3",
    "arch": "aarch64",
    "networks": [
        {"source": "z2m", "label": "Zigbee2MQTT", "version": "2.6.2", "coordinator": "ember", "devices": 12},
        {"source": "zha", "label": "ZHA", "version": "", "coordinator": "", "devices": 3},
    ],
    "broker": "Mosquitto broker app",
    "integration": True,
    "language": "en",
    "browser": "Chrome 141 on macOS",
}


def query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def template_ids(name: str) -> set[str]:
    form = yaml.safe_load((TEMPLATES / name).read_text(encoding="utf-8"))
    return {item["id"] for item in form["body"] if "id" in item}


def test_bug_link_fills_the_bug_form() -> None:
    r = feedback.build(
        "bug", {"title": "Alert repeats", "what_happened": "It repeats", "steps": "1. wait"}, ENV, [], []
    )
    q = query(r["url"])
    assert q["template"] == "bug_report.yml"
    assert q["title"] == "[Bug]: Alert repeats"
    assert q["app_version"] == "2.4.0"
    assert q["ha_version"] == "2026.11.2"
    assert q["z2m_version"] == "2.6.2"
    assert q["broker"] == "Mosquitto broker app"
    assert "Zigbee2MQTT 2.6.2, ember adapter, 12 devices" in q["setup"]
    assert "ZHA, 3 devices" in q["setup"]
    assert "Home Assistant 2026.11.2 on Home Assistant OS 16.3" in q["setup"]
    assert "expected" not in q  # empty fields stay out of the link


def test_every_prefilled_field_exists_on_its_form() -> None:
    form = {
        "title": "t",
        "what_happened": "w",
        "expected": "e",
        "steps": "s",
        "problem": "p",
        "solution": "s",
        "alternatives": "a",
    }
    for kind, template in (("bug", "bug_report.yml"), ("feature", "feature_request.yml")):
        r = feedback.build(kind, form, ENV, ["2026-11-23 10:00:00 WARNING x"], [])
        fields = set(query(r["url"])) - {"template", "title"}
        assert fields <= template_ids(template), fields - template_ids(template)


def test_feature_link_and_prefix() -> None:
    r = feedback.build("feature", {"title": "Router history", "problem": "I can't see it"}, ENV, ["log"], [])
    q = query(r["url"])
    assert q["template"] == "feature_request.yml"
    assert q["title"] == "[FR]: Router history"
    assert "logs" not in q and "app_version" not in q
    assert "Z2M Device Tracker 2.4.0" in q["setup"]


def test_required_fields_hold_back_the_link() -> None:
    r = feedback.build("bug", {"title": "Only a title"}, ENV, [], [])
    assert r["url"] == ""
    assert r["missing"] == ["what_happened"]
    assert feedback.build("feature", {}, ENV, [], [])["missing"] == ["title", "problem"]


def test_setup_and_logs_can_be_left_out() -> None:
    r = feedback.build(
        "bug", {"title": "t", "what_happened": "w"}, ENV, ["line"], [], include_setup=False, include_logs=False
    )
    q = query(r["url"])
    assert not {"setup", "logs", "app_version", "ha_version", "z2m_version", "broker"} & set(q)


def test_names_addresses_and_secrets_are_taken_out() -> None:
    logs = [
        "2026-11-23 10:00:00 WARNING Kitchen Motion (0x00158d0000500001) went offline",
        "2026-11-23 10:01:00 ERROR notify.mobile_app_sams_iphone failed for sam@example.com token=abc123",
    ]
    r = feedback.build(
        "bug", {"title": "Kitchen motion alert", "what_happened": "see log"}, ENV, logs, ["Kitchen Motion"]
    )
    q = query(r["url"])
    text = q["logs"] + q["title"]
    for secret in (
        "Kitchen Motion",
        "Kitchen motion",
        "0x00158d0000500001",
        "sams_iphone",
        "sam@example.com",
        "abc123",
    ):
        assert secret not in text
    assert "[device 1]" in q["logs"] and "[ieee]" in q["logs"]


def test_oldest_log_lines_go_first_when_the_link_is_too_long() -> None:
    logs = [f"2026-11-23 10:{i:02d}:00 WARNING {'x' * 400}" for i in range(30)]
    r = feedback.build("bug", {"title": "t", "what_happened": "w"}, ENV, logs, [])
    assert r["url"] and len(r["url"]) <= feedback.URL_MAX
    assert 0 < r["logs_used"] < 30 and r["logs_dropped"] == 30 - r["logs_used"]
    assert query(r["url"])["logs"].endswith(logs[-1][-50:])


def test_too_long_text_withholds_the_link_but_keeps_markdown() -> None:
    form = {"title": "t", "what_happened": "w " * 2000, "expected": "e " * 2000, "steps": "s " * 2000}
    r = feedback.build("bug", form, ENV, [], [])
    assert r["url"] == "" and r["over"] > 0
    assert r["markdown"].startswith("## [Bug]: t") and "### Setup" in r["markdown"]


def test_zha_only_install_says_zigbee2mqtt_is_not_used() -> None:
    env = {**ENV, "networks": [ENV["networks"][1]], "broker": ""}
    q = query(feedback.build("bug", {"title": "t", "what_happened": "w"}, env, [], [])["url"])
    assert q["z2m_version"] == "Not used (ZHA)"
    assert "broker" not in q


def test_log_buffer_keeps_the_last_warnings() -> None:
    buffer = feedback.LogBuffer(size=2)
    log = logging.getLogger("z2m_tracker.test_feedback")
    log.addHandler(buffer)
    try:
        log.info("not kept")
        for n in range(3):
            log.warning("warning %d", n)
    finally:
        log.removeHandler(buffer)
    lines = buffer.snapshot()
    assert len(lines) == 2
    assert lines[0].endswith("WARNING warning 1") and lines[1].endswith("WARNING warning 2")


def test_email_redaction_stays_fast_on_long_runs() -> None:
    import time

    start = time.monotonic()
    feedback.redact("+" * 20000 + " " + "a." * 10000)
    assert time.monotonic() - start < 1
    assert feedback.redact("mail sam.o+z2m@example.co.uk now") == "mail [email] now"
