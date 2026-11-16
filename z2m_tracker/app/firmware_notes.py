"""Firmware notes: your own notes against a device model and firmware version.

A note is {"id", "model", "version", "text", "added", "updated"}, kept in the history file under
`firmware_notes` (a list). Pure functions, no I/O.

Matching
--------
- A device matches a note when its model (Zigbee2MQTT's definition model) is the note's model,
  ignoring case.
- It *runs* the noted version when its software build ID, or the OTA file version Zigbee2MQTT
  reports as installed, is the note's version.
- An *update to* the noted version is on offer when its update is available and the OTA file
  version on offer is the note's version, or is the file version that devices of the same model
  report while running the noted software build (Zigbee2MQTT only knows the file version of an
  update before it is installed, so a note on "2.1.4" is linked to file version 33 once one
  device of that model runs 2.1.4).
- Such an update is left out of "Update all" and the overnight automatic updates. Updating that
  one device by hand ("Update anyway") still works.
"""

from __future__ import annotations

import re
import secrets
from typing import Any

from health import INFO, KIND_LABELS, active_mutes

MAX_NOTES = 200
MAX_TEXT = 500
MAX_VERSION = 40
MAX_MODEL = 80
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _line(value: Any, limit: int) -> str:
    return " ".join(_CONTROL.sub(" ", str(value or "")).split())[:limit]


def clean_notes(value: Any) -> list[dict]:
    """The stored list, keeping only well-formed notes (older files have none)."""
    if not isinstance(value, list):
        return []
    out = []
    for note in value:
        if isinstance(note, dict) and note.get("id") and note.get("model") and note.get("version"):
            out.append(note)
    return out[:MAX_NOTES]


def save_note(notes: list[dict], raw: Any, now_iso: str) -> tuple[dict | None, str | None]:
    """Add a note, or change the one with raw["id"]. Returns (note, error). Changes `notes` in place."""
    if not isinstance(raw, dict):
        return None, "Invalid note"
    model = _line(raw.get("model"), MAX_MODEL)
    version = _line(raw.get("version"), MAX_VERSION)
    text = _CONTROL.sub(" ", str(raw.get("text") or "")).strip()[:MAX_TEXT]
    if not model:
        return None, "Choose a model"
    if not version:
        return None, "Enter a firmware version"
    if not text:
        return None, "Write the note"
    note_id = str(raw.get("id") or "")
    if note_id:
        for note in notes:
            if note["id"] == note_id:
                note.update(model=model, version=version, text=text, updated=now_iso)
                return note, None
        return None, "No such note"
    if len(notes) >= MAX_NOTES:
        return None, f"At most {MAX_NOTES} notes"
    note = {"id": secrets.token_hex(6), "model": model, "version": version, "text": text, "added": now_iso}
    notes.append(note)
    return note, None


def delete_note(notes: list[dict], note_id: str) -> bool:
    before = len(notes)
    notes[:] = [note for note in notes if note["id"] != note_id]
    return len(notes) != before


def _version(value: Any) -> str | None:
    return None if value in (None, -1, "") else str(value)


def _same_model(note: dict, record: dict) -> bool:
    model = record.get("model")
    return bool(model) and str(model).lower() == str(note["model"]).lower()


def _candidates(devices: dict[str, dict]) -> list[dict]:
    return [r for r in devices.values() if r.get("status") == "active" and r.get("type") != "Coordinator"]


def runs(note: dict, record: dict) -> bool:
    update = record.get("update") or {}
    installed = {_version(record.get("software_build_id")), _version(update.get("installed_version"))} - {None}
    return _same_model(note, record) and note["version"] in installed


def aliases(note: dict, devices: dict[str, dict]) -> set[str]:
    """The note's version plus the OTA file versions seen on devices of its model running that build."""
    found = {note["version"]}
    for record in _candidates(devices):
        if _same_model(note, record) and _version(record.get("software_build_id")) == note["version"]:
            installed = _version((record.get("update") or {}).get("installed_version"))
            if installed:
                found.add(installed)
    return found


def offered(note: dict, record: dict, devices: dict[str, dict], known: set[str] | None = None) -> bool:
    update = record.get("update") or {}
    if not _same_model(note, record) or update.get("state") != "available" or runs(note, record):
        return False
    latest = _version(update.get("latest_version"))
    return latest is not None and latest in (known if known is not None else aliases(note, devices))


def matches(notes: list[dict], devices: dict[str, dict]) -> dict[str, dict[str, list[str]]]:
    """ieee -> {"runs": [note ids], "offered": [note ids]} for devices that match a note."""
    out: dict[str, dict[str, list[str]]] = {}
    candidates = _candidates(devices)
    for note in notes:
        known = aliases(note, devices)
        for record in candidates:
            if runs(note, record):
                out.setdefault(record["ieee"], {"runs": [], "offered": []})["runs"].append(note["id"])
            elif offered(note, record, devices, known):
                out.setdefault(record["ieee"], {"runs": [], "offered": []})["offered"].append(note["id"])
    return out


def ota_block(notes: list[dict], record: dict | None, devices: dict[str, dict]) -> str | None:
    """Why the update on offer shouldn't go in "Update all" or the overnight run, or None."""
    if not record:
        return None
    for note in notes:
        if offered(note, record, devices):
            return f"You noted problems with {note['version']}"
    return None


def note_issues(notes: list[dict], devices: dict[str, dict], options: dict, now: float) -> list[dict[str, Any]]:
    """The firmware_note check: a device running a version you wrote a note about. Low priority."""
    if not notes or "firmware_note" in set(options.get("disabled_checks") or []):
        return []
    issues = []
    for record in _candidates(devices):
        if "firmware_note" in active_mutes(record, now):
            continue
        note = next((n for n in notes if runs(n, record)), None)
        if note is None:
            continue
        issue = {
            "key": f"firmware_note:{record['ieee']}",
            "kind": "firmware_note",
            "label": KIND_LABELS["firmware_note"],
            "severity": INFO,
            "ieee": record["ieee"],
            "title": "Runs firmware you noted",
            "detail": f"Your note on {note['version']}: “{note['text']}”",
            "msg": "firmware_note",
            "params": {"version": note["version"], "text": note["text"], "note": note["id"]},
        }
        if record.get("critical"):
            issue["critical"] = True
        issues.append(issue)
    return issues
