"""Community device ratings: what other Zigbee Tracker users think of each device model.

Reports are GitHub issues opened from the sidebar's "Share your experience" dialog. The app never
posts anything itself: it builds a link to a pre-filled issue form and the user submits it while
signed in to GitHub. scripts/community_ratings.py checks each new issue and rebuilds
community_ratings.json from them once a week. The file is bundled with the app (works offline) and,
unless community_ratings_fetch is off, refreshed once a day from the repository.

This module holds the report format (shared with the script, so both always agree), the loader,
the lookup by vendor + model and the "rated Avoid by others" health check.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import statistics
import threading
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from health import INFO, KIND_LABELS, active_mutes

_LOGGER = logging.getLogger("z2m_tracker")

REPO = "tempus2016/z2m-device-tracker"
RAW_URL = f"https://raw.githubusercontent.com/{REPO}/main/z2m_tracker/app/community_ratings.json"
ISSUE_URL = f"https://github.com/{REPO}/issues/new"
ISSUES_URL = f"https://github.com/{REPO}/issues"
TEMPLATE = "device_rating.yml"
LABEL = "rating"
SCHEMA = 1
FETCH_INTERVAL = 86400
FETCH_RETRY = 3600
FETCH_TIMEOUT = 20
MAX_FILE_BYTES = 5_000_000

VERDICTS = ("recommend", "ok", "avoid")
# Tag keys in the order the dialog shows them. Labels live in the sidebar's i18n files (community.tag.<key>).
POSITIVE_TAGS = ("pairs_easily", "stable", "long_battery", "fast_response", "good_router", "accurate")
NEGATIVE_TAGS = (
    "hard_to_pair",
    "drops_off",
    "eats_batteries",
    "slow_response",
    "poor_router",
    "inaccurate",
    "needs_ota",
)
TAGS = POSITIVE_TAGS + NEGATIVE_TAGS
# A verdict is only shown once a model has this many reports, so one person can't decide it.
MIN_REPORTS = 5
RECOMMEND_SHARE = 0.7
AVOID_SHARE = 0.4
# Best firmware: the version with the highest recommend share among versions with this many reports.
MIN_FIRMWARE_REPORTS = 3
MAX_COMMENTS = 3
MAX_COMMENT = 500
MAX_VALUE = 80

# Report fields in the order they're written. Stats fields only appear when the user shares them.
BASE_FIELDS = ("schema", "vendor", "model", "firmware", "z2m", "adapter", "verdict", "tags", "devices", "months")
STAT_FIELDS = ("uptime", "dropouts_per_month", "lqi", "battery_months")
OPTIONAL_FIELDS = {"firmware", "z2m", "adapter", "tags"}
RANGES = {
    "devices": (1, 500),
    "months": (0, 240),
    "uptime": (0, 100),
    "dropouts_per_month": (0, 1000),
    "lqi": (0, 255),
    "battery_months": (0, 240),
}
REPORTER_RE = re.compile(r"^[0-9a-f]{16}$")
HEADING_RE = re.compile(r"^###\s+(.+?)\s*$", re.M)
NO_RESPONSE = "_No response_"


def _norm(text: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def model_key(vendor: Any, model: Any) -> str:
    """How models are matched: vendor and model with case, spaces and punctuation ignored."""
    return f"{_norm(vendor)}|{_norm(model)}"


def record_model(record: dict) -> tuple[str, str] | None:
    """The vendor and model a device record reports, as Zigbee2MQTT names them."""
    model = record.get("model") or record.get("model_id")
    if not model:
        return None
    return str(record.get("vendor") or record.get("manufacturer") or ""), str(model)


def clean_value(value: Any, limit: int = MAX_VALUE) -> str:
    """One line of plain text: no newlines or control characters, trimmed to `limit`."""
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value if value is not None else "")).strip()
    return text[:limit].strip()


def clean_comment(value: Any) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\x00-\x09\x0b-\x1f\x7f]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:MAX_COMMENT].strip()


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


# ------------------------------------------------------------------ report text


def report_text(fields: list[tuple[str, Any]]) -> str:
    """The machine-readable block posted in the issue: one `key: value` per line."""
    lines = []
    for key, value in fields:
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(v) for v in value)
        lines.append(f"{key}: {clean_value(value)}")
    return "\n".join(lines)


def parse_report(text: str) -> dict[str, str]:
    """Reads a report block back. Unknown keys are kept for validate() to ignore."""
    out: dict[str, str] = {}
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("```") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        if key and key not in out:
            out[key] = value.strip()
    return out


def parse_issue(body: str) -> dict[str, str]:
    """Splits an issue-form body into its `### Heading` sections, keyed by lower-case heading."""
    body = str(body or "").replace("\r\n", "\n")
    sections: dict[str, str] = {}
    matches = list(HEADING_RE.finditer(body))
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        value = body[match.end() : end].strip()
        if value == NO_RESPONSE:
            value = ""
        sections[match.group(1).strip().lower()] = value
    return sections


def validate(report: dict[str, str]) -> tuple[dict[str, Any] | None, list[str]]:
    """Checks a parsed report. Returns the clean report, or None and the reasons it was refused."""
    errors: list[str] = []
    if report.get("schema") != str(SCHEMA):
        errors.append(f"schema must be {SCHEMA}")
    clean: dict[str, Any] = {"schema": SCHEMA}
    for key in ("vendor", "model"):
        value = clean_value(report.get(key))
        if not value or not _norm(value):
            errors.append(f"{key} is missing")
        clean[key] = value
    for key in ("firmware", "z2m", "adapter"):
        value = clean_value(report.get(key), 40)
        if value:
            clean[key] = value
    verdict = (report.get("verdict") or "").strip().lower()
    if verdict not in VERDICTS:
        errors.append("verdict must be one of " + ", ".join(VERDICTS))
    clean["verdict"] = verdict
    tags = [t.strip().lower() for t in (report.get("tags") or "").split(",") if t.strip()]
    unknown = [t for t in tags if t not in TAGS]
    if unknown:
        errors.append("unknown tags")
    clean["tags"] = sorted(set(tags) & set(TAGS), key=TAGS.index)
    for key, (low, high) in RANGES.items():
        if key not in report:
            if key in ("devices", "months"):
                errors.append(f"{key} is missing")
            continue
        number = _number(report.get(key))
        if number is None or not low <= number <= high:
            errors.append(f"{key} must be a number from {low} to {high}")
            continue
        clean[key] = int(number) if key in ("devices", "months") else round(number, 2)
    reporter = (report.get("reporter") or "").strip().lower()
    if not REPORTER_RE.match(reporter):
        errors.append("reporter must be 16 hex characters")
    clean["reporter"] = reporter
    return (None, errors) if errors else (clean, [])


# ------------------------------------------------------------------ aggregation


def verdict_of(counts: dict[str, int]) -> str | None:
    total = sum(counts.values())
    if total < MIN_REPORTS:
        return None
    if counts.get("recommend", 0) / total >= RECOMMEND_SHARE:
        return "recommended"
    if counts.get("avoid", 0) / total >= AVOID_SHARE:
        return "avoid"
    return "mixed"


def _median(values: list[float], digits: int = 1) -> float | None:
    return round(statistics.median(values), digits) if values else None


def _version_key(version: str) -> tuple:
    return tuple((0, int(p)) if p.isdigit() else (1, p) for p in re.split(r"[.\-_ ]+", version.lower()) if p)


def aggregate(reports: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """One entry per model from validated reports (newest first, already one per person per model).

    Each report also carries `date` (YYYY-MM-DD) and its optional `comment`.
    """
    groups: dict[str, list[dict]] = {}
    for report in reports:
        groups.setdefault(model_key(report["vendor"], report["model"]), []).append(report)
    models = {}
    for key, items in groups.items():
        counts = {v: sum(1 for r in items if r["verdict"] == v) for v in VERDICTS}
        tags: dict[str, int] = {}
        for r in items:
            for tag in r.get("tags") or []:
                tags[tag] = tags.get(tag, 0) + 1
        with_stats = [r for r in items if any(k in r for k in STAT_FIELDS)]
        by_firmware: dict[str, list[str]] = {}
        for r in items:
            if r.get("firmware"):
                by_firmware.setdefault(r["firmware"], []).append(r["verdict"])
        ranked = [
            (sum(1 for v in verdicts if v == "recommend") / len(verdicts), _version_key(fw), fw)
            for fw, verdicts in by_firmware.items()
            if len(verdicts) >= MIN_FIRMWARE_REPORTS
        ]
        # Most common spelling of the vendor and model among the reports, for display.
        vendor = statistics.mode([r["vendor"] for r in items])
        model = statistics.mode([r["model"] for r in items])
        models[key] = {
            "vendor": vendor,
            "model": model,
            "reports": len(items),
            "counts": counts,
            "verdict": verdict_of(counts),
            "tags": dict(sorted(tags.items(), key=lambda kv: (-kv[1], TAGS.index(kv[0])))),
            "stats": {
                "samples": len(with_stats),
                "uptime": _median([r["uptime"] for r in items if "uptime" in r], 2),
                "dropouts_per_month": _median([r["dropouts_per_month"] for r in items if "dropouts_per_month" in r]),
                "lqi": _median([r["lqi"] for r in items if "lqi" in r], 0),
                "battery_months": _median([r["battery_months"] for r in items if "battery_months" in r]),
            },
            "best_firmware": max(ranked)[2] if ranked else None,
            "adapters": sorted({r["adapter"] for r in items if r.get("adapter")}),
            "comments": [
                {
                    "verdict": r["verdict"],
                    "text": r["comment"],
                    "firmware": r.get("firmware"),
                    "adapter": r.get("adapter"),
                    "date": r.get("date"),
                }
                for r in items
                if r.get("comment")
            ][:MAX_COMMENTS],
            "latest": max((r.get("date") or "" for r in items), default="") or None,
        }
    return dict(sorted(models.items()))


def ratings_file(models: dict[str, dict], updated: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "updated": updated,
        "source": f"https://github.com/{REPO}/issues?q=label%3A{LABEL}",
        "licence": "CC0-1.0",
        "min_reports": MIN_REPORTS,
        "models": models,
    }


# ------------------------------------------------------------------ app side


def _valid_file(data: Any) -> bool:
    return (
        isinstance(data, dict)
        and data.get("schema") == SCHEMA
        and isinstance(data.get("models"), dict)
        and all(isinstance(v, dict) for v in data["models"].values())
    )


class CommunityRatings:
    """The ratings file: the bundled copy, or the newer one fetched from GitHub (cached in /data)."""

    def __init__(self, bundled: Path, cache: Path, id_path: Path) -> None:
        self.cache = cache
        self.id_path = id_path
        self.data: dict[str, Any] = {"schema": SCHEMA, "updated": None, "models": {}}
        self.by_model: dict[str, list[str]] = {}
        self.etag: str | None = None
        self.fetched_at: float = 0.0
        self.fetch_error: str | None = None
        self._id: str | None = None
        self._lock = threading.Lock()
        for path in (bundled, cache):
            self._load(path)

    def _load(self, path: Path) -> None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if _valid_file(data) and str(data.get("updated") or "") >= str(self.data.get("updated") or ""):
            self._use(data)

    def _use(self, data: dict) -> None:
        by_model: dict[str, list[str]] = {}
        for key in data["models"]:
            by_model.setdefault(key.split("|", 1)[-1], []).append(key)
        # One assignment each, so a reader on another thread sees the old or the new file, never half.
        self.by_model = by_model
        self.data = data

    @property
    def updated(self) -> str | None:
        return self.data.get("updated")

    def key_for(self, record: dict) -> str | None:
        """The ratings key for a device: vendor + model, or the model alone when only one vendor has it."""
        found = record_model(record)
        if found is None:
            return None
        models = self.data["models"]
        key = model_key(*found)
        if key in models:
            return key
        others = self.by_model.get(_norm(found[1])) or []
        if len(others) == 1 and _norm(found[1]):
            return others[0]
        return None

    def lookup(self, record: dict) -> dict | None:
        key = self.key_for(record)
        return self.data["models"].get(key) if key else None

    def owned(self, records: list[dict]) -> dict[str, dict]:
        """Entries for the models on this install's networks, each with the IEEEs that have it."""
        out: dict[str, dict] = {}
        for record in records:
            if record.get("status") != "active" or record.get("type") == "Coordinator":
                continue
            key = self.key_for(record)
            if key is None:
                continue
            entry = out.setdefault(key, {**self.data["models"][key], "ieees": []})
            entry["ieees"].append(record["ieee"])
        return out

    def snapshot(self, records: list[dict], fetch_enabled: bool) -> dict[str, Any]:
        return {
            "updated": self.updated,
            "total": len(self.data["models"]),
            "min_reports": MIN_REPORTS,
            "fetch": fetch_enabled,
            "models": self.owned(records),
        }

    def all_models(self) -> dict[str, Any]:
        return {"updated": self.updated, "min_reports": MIN_REPORTS, "models": self.data["models"]}

    # -------------------------------------------------------------- fetch

    def fetch_due(self, now: float) -> bool:
        wait = FETCH_RETRY if self.fetch_error else FETCH_INTERVAL
        return now - self.fetched_at >= wait

    def fetch(self, now: float) -> bool:
        """Downloads the file from GitHub. True when a newer one was taken."""
        self.fetched_at = now
        request = urllib.request.Request(RAW_URL, headers={"User-Agent": "z2m-tracker", "Accept": "application/json"})
        if self.etag:
            request.add_header("If-None-Match", self.etag)
        try:
            with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:  # noqa: S310 - fixed https URL
                body = response.read(MAX_FILE_BYTES + 1)
                etag = response.headers.get("ETag")
        except urllib.error.HTTPError as err:
            if err.code == 304:
                self.fetch_error = None
                return False
            self.fetch_error = f"HTTP {err.code}"
            return False
        except (OSError, ValueError) as err:
            self.fetch_error = str(err) or type(err).__name__
            return False
        if len(body) > MAX_FILE_BYTES:
            self.fetch_error = "file too large"
            return False
        try:
            data = json.loads(body.decode("utf-8"))
        except ValueError:
            self.fetch_error = "not JSON"
            return False
        if not _valid_file(data):
            self.fetch_error = "unexpected format"
            return False
        self.fetch_error = None
        self.etag = etag
        if str(data.get("updated") or "") <= str(self.updated or ""):
            return False
        self._use(data)
        try:
            tmp = self.cache.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self.cache)
        except OSError as err:
            _LOGGER.warning("Couldn't save the community ratings (%s)", err)
        return True

    # -------------------------------------------------------------- reports

    def install_id(self) -> str:
        """A random ID kept in /data. Only its hash with a model ever leaves the app."""
        with self._lock:
            if self._id:
                return self._id
            try:
                value = self.id_path.read_text(encoding="utf-8").strip()
            except OSError:
                value = ""
            if not re.fullmatch(r"[0-9a-f]{32}", value):
                value = secrets.token_hex(16)
                try:
                    self.id_path.write_text(value, encoding="utf-8")
                except OSError as err:
                    _LOGGER.warning("Couldn't save the community install ID (%s)", err)
            self._id = value
            return value

    def reporter(self, key: str) -> str:
        """Same for every report this install makes about one model, so a newer report replaces the older."""
        return hashlib.sha256(f"{self.install_id()}:{key}".encode()).hexdigest()[:16]


def _months(seconds: float) -> float:
    return seconds / (86400 * 30.44)


def _first_seen(record: dict) -> float | None:
    try:
        return datetime.fromisoformat(str(record.get("first_seen"))).timestamp()
    except ValueError:
        return None


def build_report(
    records: list[dict],
    bridge: dict,
    reliability: dict | None,
    lqis: list[float],
    reporter: str,
    choice: dict,
    now: float,
) -> tuple[list[tuple[str, Any]], str, list[str]]:
    """The report for one model from this install. Returns the fields, the comment and any problems.

    `records` are this install's active devices of the model, `bridge` the Zigbee2MQTT info of the
    first one's network, `reliability` its row from model_reliability, `lqis` each device's recent mean LQI.
    `choice` is what the user picked: verdict, tags, comment, stats (bool).
    """
    problems = []
    verdict = str(choice.get("verdict") or "")
    if verdict not in VERDICTS:
        problems.append("verdict")
    raw_tags = choice.get("tags") if isinstance(choice.get("tags"), list) else []
    tags = [t for t in TAGS if t in raw_tags]
    first = min((s for s in (_first_seen(r) for r in records) if s is not None), default=now)
    vendor, model = record_model(records[0]) or ("", "")
    firmware = next((r.get("software_build_id") for r in records if r.get("software_build_id")), None)
    fields: list[tuple[str, Any]] = [
        ("schema", SCHEMA),
        ("vendor", vendor),
        ("model", model),
    ]
    if firmware:
        fields.append(("firmware", clean_value(firmware, 40)))
    if bridge.get("version"):
        fields.append(("z2m", clean_value(bridge["version"], 40)))
    if bridge.get("coordinator_type"):
        fields.append(("adapter", clean_value(bridge["coordinator_type"], 40)))
    fields += [("verdict", verdict), ("tags", tags), ("devices", len(records)), ("months", int(_months(now - first)))]
    if choice.get("stats"):
        rel = reliability or {}
        if rel.get("uptime_30d") is not None:
            fields.append(("uptime", rel["uptime_30d"]))
        if rel.get("dropouts_per_month") is not None:
            fields.append(("dropouts_per_month", rel["dropouts_per_month"]))
        if lqis:
            fields.append(("lqi", round(statistics.median(lqis))))
        if rel.get("battery_life_days"):
            fields.append(("battery_months", round(_months(rel["battery_life_days"] * 86400), 1)))
    fields.append(("reporter", reporter))
    return fields, clean_comment(choice.get("comment")), problems


def issue_url(fields: list[tuple[str, Any]], comment: str) -> str:
    values = dict(fields)
    title = f"Rating: {values.get('vendor', '')} {values.get('model', '')} ({values.get('verdict', '')})".strip()
    query = {"template": TEMPLATE, "title": title, "report": report_text(fields)}
    if comment:
        query["comment"] = comment
    return f"{ISSUE_URL}?{urlencode(query)}"


# ------------------------------------------------------------------ health check


def community_issues(records: list[dict], ratings: CommunityRatings, options: dict, now: float) -> list[dict]:
    """Info issue for each active device whose model other users rate Avoid."""
    if "community_avoid" in set(options.get("disabled_checks") or []):
        return []
    issues = []
    for record in records:
        if record.get("status") != "active" or record.get("type") == "Coordinator":
            continue
        entry = ratings.lookup(record)
        if not entry or entry.get("verdict") != "avoid":
            continue
        if "community_avoid" in active_mutes(record, now):
            continue
        avoid = int((entry.get("counts") or {}).get("avoid", 0))
        total = int(entry.get("reports") or 0)
        bad = [t for t in entry.get("tags") or {} if t in NEGATIVE_TAGS][:2]
        model = f"{entry.get('vendor', '')} {entry.get('model', '')}".strip()
        detail = f"{avoid} of {total} other users rate {model} Avoid."
        issue = {
            "key": f"community_avoid:{record['ieee']}",
            "kind": "community_avoid",
            "label": KIND_LABELS["community_avoid"],
            "severity": INFO,
            "ieee": record["ieee"],
            "title": "Rated poorly by others",
            "detail": detail,
            "msg": "community_avoid",
            "params": {"model": model, "avoid": avoid, "total": total, "tags": bad},
        }
        if record.get("critical"):
            issue["critical"] = True
        issues.append(issue)
    return issues
