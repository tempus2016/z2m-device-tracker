"""Device naming: the naming pattern setting, the friendly device kind, and the naming check.

A pattern such as "{area} {type} {n}" builds names like "Kitchen Motion 1". Tokens:
{area} (Home Assistant area), {type} (friendly kind, see kind_of), {model} (Zigbee2MQTT model)
and {n} (the lowest number that makes the name unique). The sidebar has the same rules in JS
(renderPattern in www/index.html); keep the two in step.
"""

from __future__ import annotations

import re
from typing import Any

from health import INFO, KIND_LABELS, active_mutes

TOKENS = ("area", "type", "model", "n")
TOKEN_RE = re.compile(r"\{([a-z]+)\}")
PATTERN_MAX = 80
NAME_MAX = 120
IEEE_NAME = re.compile(r"^0x[0-9a-f]{16}$", re.IGNORECASE)
# Separators trimmed from the ends, and collapsed when an empty token leaves two side by side.
SEPARATORS = "-_·,:|"
_EDGE = re.compile(rf"^[\s{re.escape(SEPARATORS)}/]+|[\s{re.escape(SEPARATORS)}/]+$")
_DOUBLE_SEP = re.compile(rf"([{re.escape(SEPARATORS)}])(\s*[{re.escape(SEPARATORS)}])+")

# Exposed properties (or light/switch/... types) to a kind, first match wins.
FEATURE_KINDS = [
    ("water_leak", "Leak"),
    ("smoke", "Smoke"),
    ("gas", "Gas"),
    ("carbon_monoxide", "CO"),
    ("occupancy", "Motion"),
    ("presence", "Presence"),
    ("contact", "Door"),
    ("vibration", "Vibration"),
    ("climate", "Thermostat"),
    ("local_temperature", "Thermostat"),
    ("cover", "Blind"),
    ("lock", "Lock"),
    ("fan", "Fan"),
    ("light", "Bulb"),
    ("brightness", "Bulb"),
    ("action", "Button"),
]
# Words in the Zigbee2MQTT description (or model) to a kind, for devices whose exposes say little.
DESCRIPTION_KINDS = [
    ("leak", "Leak"),
    ("smoke", "Smoke"),
    ("motion", "Motion"),
    ("presence", "Presence"),
    ("door", "Door"),
    ("window", "Door"),
    ("contact", "Door"),
    ("vibration", "Vibration"),
    ("thermostat", "Thermostat"),
    ("radiator", "Thermostat"),
    ("blind", "Blind"),
    ("curtain", "Blind"),
    ("bulb", "Bulb"),
    ("light", "Bulb"),
    ("plug", "Plug"),
    ("socket", "Plug"),
    ("outlet", "Plug"),
    ("button", "Button"),
    ("remote", "Button"),
    ("dimmer", "Switch"),
    ("switch", "Switch"),
    ("temperature", "Temp"),
    ("humidity", "Temp"),
    ("air quality", "Air"),
    ("repeater", "Repeater"),
    ("router", "Repeater"),
]
AIR = ("pm25", "co2", "voc", "formaldehyd", "air_quality")


def clean_pattern(value: Any) -> str:
    """Validate the naming_pattern setting. Empty turns the pattern off."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("enter text like {area} {type}")
    pattern = " ".join(value.split())
    if not pattern:
        return ""
    if len(pattern) > PATTERN_MAX:
        raise ValueError(f"use at most {PATTERN_MAX} characters")
    if re.search(r"[+#]", pattern) or pattern.endswith("/"):
        raise ValueError("no + or # and no trailing slash")
    unknown = [t for t in TOKEN_RE.findall(pattern) if t not in TOKENS]
    if unknown:
        raise ValueError(f"unknown token {{{unknown[0]}}}: use {{area}}, {{type}}, {{model}} or {{n}}")
    if "{" in TOKEN_RE.sub("", pattern) or "}" in TOKEN_RE.sub("", pattern):
        raise ValueError("write each token as {area}, {type}, {model} or {n}")
    if not {"area", "type", "model"} & set(TOKEN_RE.findall(pattern)):
        raise ValueError("include {area}, {type} or {model}")
    return pattern


def kind_of(record: dict) -> str | None:
    """A short friendly kind (Motion, Door, Plug, Bulb, ...) from exposes and description, or None."""
    if record.get("type") == "Coordinator":
        return None
    features = [str(f).lower() for f in record.get("features") or []]
    has = set(features)
    for prop, kind in FEATURE_KINDS:
        if prop in has:
            return kind
    if has & {"state", "switch"}:
        return "Plug" if has & {"power", "energy", "current", "voltage"} else "Switch"
    if any(f.startswith(AIR) for f in features):
        return "Air"
    if has & {"temperature", "humidity", "pressure"}:
        return "Temp"
    if "illuminance" in has or "illuminance_lux" in has:
        return "Light sensor"
    text = f"{record.get('description') or ''} {record.get('model') or ''}".lower()
    for word, kind in DESCRIPTION_KINDS:
        if word in text:
            return kind
    if record.get("type") == "Router":
        return "Repeater"
    return None


def _value(text: Any) -> str:
    """A token value made safe for a Zigbee2MQTT name: no topic levels, wildcards or extra spaces."""
    return _tidy(re.sub(r"[+#]", "", str(text or "").replace("/", "-")))


def values_of(record: dict) -> dict[str, str]:
    return {"area": _value(record.get("area")), "type": _value(kind_of(record)), "model": _value(record.get("model"))}


def _tidy(text: str) -> str:
    text = " ".join(text.split())
    text = _DOUBLE_SEP.sub(r"\1", text)
    text = " ".join(_EDGE.sub("", text).split())
    # An empty token can leave "Z : Bulb"; no space belongs before these marks.
    return re.sub(r"\s+([,:;.])", r"\1", text)


def render(pattern: str, values: dict[str, str], taken: set[str]) -> str | None:
    """The name the pattern gives, unique against `taken` (lower-case names), or None.

    Empty tokens are left out. With {n}, the lowest free number from 1; without it, a clash
    gets " 2", " 3", ... added.
    """
    if not pattern:
        return None
    base = TOKEN_RE.sub(lambda m: "\0" if m.group(1) == "n" else values.get(m.group(1), ""), pattern)
    if not _tidy(base.replace("\0", "")):
        return None
    has_n = "\0" in base
    for n in range(1, 1000):
        if has_n:
            name = _tidy(base.replace("\0", str(n)))
        else:
            name = _tidy(base) if n == 1 else f"{_tidy(base)} {n}"
        name = name[:NAME_MAX].rstrip()
        if name.lower() not in taken:
            return name
    return None


def matches(pattern: str, name: str, values: dict[str, str]) -> bool | None:
    """Whether the name follows the pattern; None when a token the pattern uses has no value."""
    used = set(TOKEN_RE.findall(pattern))
    if any(not values.get(t) for t in used - {"n"}):
        return None
    parts = []
    for piece in re.split(r"(\{[a-z]+\})", pattern):
        token = TOKEN_RE.fullmatch(piece)
        if token:
            parts.append(r"\d+" if token.group(1) == "n" else re.escape(values[token.group(1)]))
        elif piece:
            parts.append(r"\s+".join(re.escape(w) for w in piece.split(" ")))
    regex = "".join(parts)
    if "n" not in used:
        regex += r"(?:\s+\d+)?"
    return re.fullmatch(regex, " ".join(str(name).split()), re.IGNORECASE) is not None


def is_ieee_name(name: Any) -> bool:
    return bool(name) and bool(IEEE_NAME.match(str(name)))


def naming_issues(records: list[dict], options: dict, now: float) -> list[dict[str, Any]]:
    """The naming check: devices still named like an IEEE address, and names that don't follow the pattern.

    Suggested names are unique across every active device and across the suggestions themselves,
    so renaming them all one after another never clashes.
    """
    if "naming" in set(options.get("disabled_checks") or []):
        return []
    pattern = str(options.get("naming_pattern") or "")
    active = [
        r for r in records if r.get("status") == "active" and r.get("type") != "Coordinator" and r.get("friendly_name")
    ]
    taken = {str(r["friendly_name"]).lower() for r in active}
    flagged = []
    for record in active:
        if record.get("interview_state") and record.get("interview_state") != "SUCCESSFUL":
            continue
        name = str(record["friendly_name"])
        if is_ieee_name(name):
            flagged.append((record, "naming_ieee"))
        elif pattern and matches(pattern, name, values_of(record)) is False:
            flagged.append((record, "naming_pattern"))
    flagged.sort(key=lambda x: (x[0].get("first_seen") or "", x[0]["ieee"]))
    issues = []
    for record, msg in flagged:
        if "naming" in active_mutes(record, now):
            continue
        name = str(record["friendly_name"])
        suggested = None
        if pattern:
            own = name.lower()
            suggested = render(pattern, values_of(record), taken - {own})
            if suggested and suggested.lower() == own:
                suggested = None
            if suggested:
                taken.add(suggested.lower())
        hint = f" Suggested: {suggested}." if suggested else ""
        if msg == "naming_ieee":
            title, detail = "Named by its address", f"Still named by its IEEE address.{hint}"
        else:
            title, detail = "Doesn't match naming pattern", f"Doesn't follow {pattern}.{hint}"
        issue = {
            "key": f"naming:{record['ieee']}",
            "kind": "naming",
            "label": KIND_LABELS["naming"],
            "severity": INFO,
            "ieee": record["ieee"],
            "title": title,
            "detail": detail,
            "msg": msg,
            "params": {"pattern": pattern, "suggested": suggested},
        }
        if record.get("critical"):
            issue["critical"] = True
        issues.append(issue)
    return issues
