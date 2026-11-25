"""Community ratings: report format, aggregation, matching devices, the health check and the scripts."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import community
import pytest
from community import (
    CommunityRatings,
    aggregate,
    build_report,
    community_issues,
    issue_url,
    model_key,
    parse_issue,
    parse_report,
    report_text,
    validate,
    verdict_of,
)
from conftest import PLUG, SENSOR, coordinator, send, z2m_device

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("community_ratings_script", ROOT / "scripts" / "community_ratings.py")
script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(script)

REPORTER = "0123456789abcdef"


def report(**extra) -> dict:
    base = {
        "schema": "1",
        "vendor": "Sim Labs",
        "model": "SIM-MS01",
        "verdict": "recommend",
        "devices": "1",
        "months": "6",
        "reporter": REPORTER,
    }
    base.update({k: str(v) for k, v in extra.items()})
    return base


def clean(**extra) -> dict:
    value, errors = validate(report(**extra))
    assert errors == []
    return value


def issue_body(text: str, comment: str = "") -> str:
    return f"### Report\n\n```yaml\n{text}\n```\n\n### Comment\n\n{comment or '_No response_'}\n"


def ratings_with(tmp_path: Path, models: dict, updated: str = "2026-11-01T00:00:00Z") -> CommunityRatings:
    bundled = tmp_path / "bundled.json"
    bundled.write_text(json.dumps(community.ratings_file(models, updated)), encoding="utf-8")
    return CommunityRatings(bundled, tmp_path / "cache.json", tmp_path / "id")


def entry(verdict: str | None = "avoid", **extra) -> dict:
    base = {
        "vendor": "FakeCo",
        "model": "FK-1",
        "reports": 6,
        "counts": {"recommend": 1, "ok": 1, "avoid": 4},
        "verdict": verdict,
        "tags": {"drops_off": 4, "pairs_easily": 2, "poor_router": 1},
    }
    base.update(extra)
    return base


# ------------------------------------------------------------------ format


def test_report_round_trips_through_an_issue_body():
    fields = [
        ("schema", 1),
        ("vendor", "Sim Labs"),
        ("model", "SIM-MS01"),
        ("verdict", "ok"),
        ("tags", ["stable", "drops_off"]),
    ]
    fields += [("devices", 2), ("months", 14), ("lqi", 118), ("reporter", REPORTER)]
    sections = parse_issue(issue_body(report_text(fields), "Works fine."))
    parsed = parse_report(sections["report"])
    value, errors = validate(parsed)
    assert errors == []
    assert value["tags"] == ["stable", "drops_off"]
    assert value["devices"] == 2 and value["lqi"] == 118
    assert sections["comment"] == "Works fine."


def test_no_response_comment_reads_as_empty():
    assert parse_issue(issue_body("schema: 1"))["comment"] == ""


def test_plain_text_report_without_the_form_is_read():
    # Pasted without the issue form (#90): no ### headings, comment after a "Comment:" line.
    body = report_text(list(report().items())) + "\n\nComment:\nAlso sold as: Sim Sensor\nmodel: other"
    sections = parse_issue(body)
    value, errors = validate(parse_report(sections["report"]))
    assert errors == []
    assert value["model"] == "SIM-MS01"
    assert sections["comment"] == "Also sold as: Sim Sensor\nmodel: other"


def test_plain_text_report_without_a_comment():
    sections = parse_issue(report_text(list(report().items())))
    assert validate(parse_report(sections["report"]))[1] == []
    assert sections["comment"] == ""


def test_values_stay_on_one_line():
    text = report_text([("vendor", "Evil\nverdict: avoid"), ("verdict", "recommend")])
    assert parse_report(text) == {"vendor": "Evil verdict: avoid", "verdict": "recommend"}


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"schema": "2"}, "schema"),
        ({"verdict": "great"}, "verdict"),
        ({"model": " "}, "model"),
        ({"tags": "stable, shiny"}, "unknown tags"),
        ({"lqi": "300"}, "lqi"),
        ({"uptime": "nan"}, "uptime"),
        ({"reporter": "xyz"}, "reporter"),
        ({"devices": "0"}, "devices"),
    ],
)
def test_validate_refuses_bad_reports(change, problem):
    value, errors = validate(report(**change))
    assert value is None
    assert any(problem in e for e in errors)


def test_validate_needs_devices_and_months():
    raw = report()
    del raw["months"]
    assert any("months" in e for e in validate(raw)[1])


# ------------------------------------------------------------------ aggregation


def test_verdict_needs_five_reports():
    assert verdict_of({"recommend": 4, "ok": 0, "avoid": 0}) is None
    assert verdict_of({"recommend": 4, "ok": 1, "avoid": 0}) == "recommended"
    assert verdict_of({"recommend": 2, "ok": 1, "avoid": 2}) == "avoid"
    assert verdict_of({"recommend": 3, "ok": 2, "avoid": 1}) == "mixed"


def test_aggregate_counts_tags_stats_and_best_firmware():
    reports = [
        clean(verdict="recommend", firmware="1.0.6", tags="stable", lqi=140, battery_months=20) for _ in range(3)
    ]
    reports += [clean(verdict="avoid", firmware="1.0.4", tags="drops_off, stable", lqi=90) for _ in range(3)]
    reports[0]["comment"], reports[0]["date"] = "Solid on 1.0.6", "2026-11-10"
    models = aggregate(reports)
    m = models[model_key("Sim Labs", "SIM-MS01")]
    assert m["reports"] == 6 and m["counts"] == {"recommend": 3, "ok": 0, "avoid": 3}
    assert m["verdict"] == "avoid"
    assert m["tags"] == {"stable": 6, "drops_off": 3}
    assert m["stats"]["samples"] == 6 and m["stats"]["lqi"] == 115 and m["stats"]["battery_months"] == 20
    assert m["best_firmware"] == "1.0.6"
    assert m["comments"] == [
        {"verdict": "recommend", "text": "Solid on 1.0.6", "firmware": "1.0.6", "adapter": None, "date": "2026-11-10"}
    ]
    assert m["latest"] == "2026-11-10"


def test_model_spelling_differences_group_together():
    models = aggregate([clean(vendor="Sim Labs", model="SIM-MS01"), clean(vendor="sim labs", model="sim ms01")])
    assert len(models) == 1


# ------------------------------------------------------------------ loader


def test_lookup_by_vendor_and_model_then_model_alone(tmp_path):
    ratings = ratings_with(tmp_path, {model_key("FakeCo", "FK-1"): entry()})
    assert ratings.lookup({"vendor": "fakeco", "model": "fk 1"})["verdict"] == "avoid"
    assert ratings.lookup({"vendor": "Other", "model": "FK-1"})["verdict"] == "avoid"
    assert ratings.lookup({"vendor": "FakeCo", "model": "FK-2"}) is None


def test_model_alone_is_not_used_when_two_vendors_share_it(tmp_path):
    ratings = ratings_with(tmp_path, {model_key("A", "X1"): entry(), model_key("B", "X1"): entry()})
    assert ratings.lookup({"vendor": "C", "model": "X1"}) is None


def test_newer_cache_wins_over_bundled_copy(tmp_path):
    (tmp_path / "cache.json").write_text(
        json.dumps(community.ratings_file({model_key("FakeCo", "FK-1"): entry("recommended")}, "2026-12-01T00:00:00Z")),
        encoding="utf-8",
    )
    ratings = ratings_with(tmp_path, {model_key("FakeCo", "FK-1"): entry()})
    assert ratings.lookup({"vendor": "FakeCo", "model": "FK-1"})["verdict"] == "recommended"


def test_broken_files_are_ignored(tmp_path):
    (tmp_path / "cache.json").write_text('{"schema": 1, "models": []}', encoding="utf-8")
    ratings = ratings_with(tmp_path, {model_key("FakeCo", "FK-1"): entry()})
    assert ratings.lookup({"vendor": "FakeCo", "model": "FK-1"}) is not None


def test_install_id_is_kept_and_reporter_is_per_model(tmp_path):
    ratings = ratings_with(tmp_path, {})
    first = ratings.reporter("a|b")
    again = CommunityRatings(tmp_path / "bundled.json", tmp_path / "cache.json", tmp_path / "id")
    assert again.reporter("a|b") == first
    assert again.reporter("a|c") != first
    assert len(first) == 16


class FakeResponse:
    def __init__(self, body: bytes, etag: str = '"v2"') -> None:
        self.body, self.headers = body, {"ETag": etag}

    def read(self, limit: int) -> bytes:
        return self.body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_fetch_takes_a_newer_file_and_caches_it(tmp_path, monkeypatch):
    ratings = ratings_with(tmp_path, {})
    newer = community.ratings_file({model_key("FakeCo", "FK-1"): entry()}, "2026-12-01T00:00:00Z")
    monkeypatch.setattr(community.urllib.request, "urlopen", lambda *a, **k: FakeResponse(json.dumps(newer).encode()))
    assert ratings.fetch(1000.0) is True
    assert ratings.updated == "2026-12-01T00:00:00Z" and ratings.etag == '"v2"'
    assert json.loads((tmp_path / "cache.json").read_text())["updated"] == "2026-12-01T00:00:00Z"
    assert not ratings.fetch_due(1000.0 + 3600) and ratings.fetch_due(1000.0 + 86400)


def test_fetch_refuses_junk_and_retries_sooner(tmp_path, monkeypatch):
    ratings = ratings_with(tmp_path, {model_key("FakeCo", "FK-1"): entry()})
    monkeypatch.setattr(community.urllib.request, "urlopen", lambda *a, **k: FakeResponse(b"<html>"))
    assert ratings.fetch(1000.0) is False
    assert ratings.fetch_error == "not JSON"
    assert ratings.lookup({"vendor": "FakeCo", "model": "FK-1"}) is not None
    assert ratings.fetch_due(1000.0 + 3600)


# ------------------------------------------------------------------ report + link


def dev(ieee: str, **extra) -> dict:
    base = {"ieee": ieee, "status": "active", "type": "EndDevice", "vendor": "FakeCo", "model": "FK-1"}
    base.update(extra)
    return base


def test_build_report_without_stats_leaves_them_out():
    records = [dev(SENSOR, first_seen="2026-01-01T00:00:00+00:00", software_build_id="1.2")]
    reliability = {"uptime_30d": 99.5, "dropouts_per_month": 0.0, "battery_life_days": 300}
    bridge = {"version": "2.6.1", "coordinator_type": "EmberZNet"}
    now = 1_780_000_000.0
    fields, comment, problems = build_report(
        records, bridge, reliability, [120.0], REPORTER, {"verdict": "ok", "tags": ["stable", "bogus"]}, now
    )
    values = dict(fields)
    assert problems == [] and comment == ""
    assert values["firmware"] == "1.2" and values["z2m"] == "2.6.1" and values["adapter"] == "EmberZNet"
    assert values["tags"] == ["stable"]
    assert not set(community.STAT_FIELDS) & set(values)
    assert "ieee" not in report_text(fields) and SENSOR not in report_text(fields)

    fields, _, _ = build_report(
        records, bridge, reliability, [120.0, 100.0], REPORTER, {"verdict": "ok", "stats": True}, now
    )
    values = dict(fields)
    assert values["uptime"] == 99.5 and values["lqi"] == 110 and values["battery_months"] == 9.9


def test_build_report_flags_a_missing_verdict():
    _, _, problems = build_report([dev(SENSOR)], {}, None, [], REPORTER, {}, 0.0)
    assert problems == ["verdict"]


def test_issue_url_prefills_the_form():
    fields = [("schema", 1), ("vendor", "FakeCo"), ("model", "FK-1"), ("verdict", "avoid"), ("reporter", REPORTER)]
    url = urlparse(issue_url(fields, "Keeps dropping & off"))
    query = parse_qs(url.query)
    assert url.netloc == "github.com" and url.path.endswith("/issues/new")
    assert query["template"] == ["device_rating.yml"]
    assert query["title"] == ["Rating: FakeCo FK-1 (avoid)"]
    assert query["comment"] == ["Keeps dropping & off"]
    assert parse_report(query["report"][0])["verdict"] == "avoid"


def test_tracker_report_covers_every_device_of_the_model(tracker, tmp_path):
    tracker.community = ratings_with(tmp_path, {})
    send(tracker, "bridge/devices", [coordinator(), z2m_device(SENSOR, "A"), z2m_device(PLUG, "B", "Router")])
    result = tracker.community_report(SENSOR, {"verdict": "recommend", "stats": True})
    values = dict(result["fields"])
    assert values["devices"] == 2 and values["model"] == "FK-1"
    assert result["url"].startswith("https://github.com/tempus2016/z2m-device-tracker/issues/new?")
    assert tracker.community_report("0x0000000000000bad", {}) is None


# ------------------------------------------------------------------ health check + snapshot


def test_avoid_rating_raises_an_info_issue(tmp_path):
    ratings = ratings_with(tmp_path, {model_key("FakeCo", "FK-1"): entry()})
    issues = community_issues([dev(SENSOR), dev(PLUG, model="OTHER")], ratings, {}, 0.0)
    assert [i["key"] for i in issues] == [f"community_avoid:{SENSOR}"]
    assert issues[0]["severity"] == "info"
    assert issues[0]["params"] == {"model": "FakeCo FK-1", "avoid": 4, "total": 6, "tags": ["drops_off", "poor_router"]}


def test_no_issue_without_an_avoid_verdict_or_when_switched_off(tmp_path):
    ratings = ratings_with(tmp_path, {model_key("FakeCo", "FK-1"): entry(None)})
    assert community_issues([dev(SENSOR)], ratings, {}, 0.0) == []
    ratings = ratings_with(tmp_path, {model_key("FakeCo", "FK-1"): entry()})
    assert community_issues([dev(SENSOR)], ratings, {"disabled_checks": ["community_avoid"]}, 0.0) == []
    assert community_issues([dev(SENSOR, status="removed")], ratings, {}, 0.0) == []


def test_snapshot_lists_owned_models_and_health_picks_it_up(tracker, tmp_path):
    tracker.community = ratings_with(
        tmp_path, {model_key("FakeCo", "FK-1"): entry(), model_key("X", "Y"): entry("recommended")}
    )
    send(tracker, "bridge/devices", [coordinator(), z2m_device(SENSOR, "A")])
    tracker.evaluate_health()
    snap = json.loads(tracker.snapshot_json())
    assert snap["community"]["total"] == 2
    assert list(snap["community"]["models"]) == [model_key("FakeCo", "FK-1")]
    assert snap["community"]["models"][model_key("FakeCo", "FK-1")]["ieees"] == [SENSOR]
    device = next(d for d in snap["devices"] if d["ieee"] == SENSOR)
    assert device["community_key"] == model_key("FakeCo", "FK-1")
    assert any(i["kind"] == "community_avoid" for i in tracker.issues)


# ------------------------------------------------------------------ scripts


def gh_issue(
    number: int, body: str, user: str = "alice", created: str = "2026-11-10T10:00:00Z", labels=("rating",)
) -> dict:
    return {
        "number": number,
        "body": body,
        "user": {"login": user},
        "created_at": created,
        "labels": [{"name": n} for n in labels],
    }


def test_check_accepts_a_good_issue_and_explains_a_bad_one(tmp_path):
    good = tmp_path / "good.json"
    good.write_text(
        json.dumps({"issue": gh_issue(1, issue_body(report_text(list(report().items()))))}), encoding="utf-8"
    )
    result = script.check(str(good))
    assert result["valid"] is True and "`Sim Labs SIM-MS01`" in result["message"]
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"issue": gh_issue(2, issue_body("schema: 1\nverdict: great"))}), encoding="utf-8")
    result = script.check(str(bad))
    assert result["valid"] is False and "verdict must be one of" in result["message"]


def test_collect_keeps_the_newest_per_person_and_model():
    text = report_text(list(report(verdict="avoid").items()))
    newer = report_text(list(report(verdict="recommend").items()))
    other = report_text(list(report(reporter="fedcba9876543210").items()))
    issues = [
        gh_issue(1, issue_body(text), created="2026-11-01T00:00:00Z"),
        gh_issue(2, issue_body(newer), created="2026-11-05T00:00:00Z"),
        gh_issue(3, issue_body(other), user="bob", created="2026-11-02T00:00:00Z"),
        gh_issue(4, issue_body(other), user="carol", labels=("rating", "rating-rejected")),
        gh_issue(5, issue_body("junk"), user="dave"),
    ]
    reports, skipped = script.collect(issues)
    assert [r["verdict"] for r in reports] == ["recommend", "recommend"]
    assert skipped == 3


def test_bundled_file_is_valid():
    data = json.loads((ROOT / "z2m_tracker" / "app" / "community_ratings.json").read_text(encoding="utf-8"))
    assert community._valid_file(data)


def test_issue_template_field_ids_match_the_link():
    template = (ROOT / ".github" / "ISSUE_TEMPLATE" / community.TEMPLATE).read_text(encoding="utf-8")
    assert "id: report" in template and "id: comment" in template
    assert f'labels: ["{community.LABEL}"]' in template
    assert "label: Report" in template and "label: Comment" in template
