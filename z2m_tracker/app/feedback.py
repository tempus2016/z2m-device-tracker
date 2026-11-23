"""Settings > Help & feedback: a bug report or feature request, pre-filled for GitHub.

The sidebar builds GitHub's ``issues/new`` link from the repo's own issue forms
(.github/ISSUE_TEMPLATE); nothing is sent from here. The person reads the text
on GitHub and submits it there, signed in to their own account.

What gets filled in is the part people leave out by hand: versions, how the
app runs, which networks it reads, the broker, and the app's own recent
warnings. Device names, IEEE addresses, e-mail addresses, phone notify
services and tokens are taken out first.
"""

from __future__ import annotations

import logging
import re
import threading
from collections import deque
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

REPO = "tempus2016/z2m-device-tracker"
ISSUE_URL = f"https://github.com/{REPO}/issues/new"
ISSUES_URL = f"https://github.com/{REPO}/issues"
# GitHub answers 414 somewhere past 8 KB; stay clear of it.
URL_MAX = 7800
TEXT_MAX = 4000
TITLE_MAX = 200
LOG_MAX = 30

KINDS = {
    # kind: (issue form, title prefix, required fields, optional fields)
    "bug": ("bug_report.yml", "[Bug]: ", ("title", "what_happened"), ("expected", "steps")),
    "feature": ("feature_request.yml", "[FR]: ", ("title", "problem"), ("solution", "alternatives")),
}

_IEEE = re.compile(r"\b0x[0-9a-fA-F]{16}\b")
# Bounded (RFC 5321 lengths) so a long run of "+" or "." can't make the match slow.
_EMAIL = re.compile(r"[\w.+-]{1,64}@[\w-]{1,63}\.[\w.-]{1,190}")
_SECRET = re.compile(r"(token|access_token|api_key|password|authSig)([=:]\s*)([^&\s\"',]+)", re.IGNORECASE)
_BEARER = re.compile(r"\bBearer\s+[\w.-]+")
# Phones and people are usually named after someone ("notify.mobile_app_sams_iphone").
_PERSONAL = re.compile(r"\b(mobile_app|person|device_tracker)([._])[\w-]+", re.IGNORECASE)


class LogBuffer(logging.Handler):
    """The app's last warnings and errors, kept in memory for a bug report."""

    def __init__(self, size: int = LOG_MAX) -> None:
        super().__init__(level=logging.WARNING)
        self.lines: deque[str] = deque(maxlen=size)
        self._lines_lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            when = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")
            line = f"{when} {record.levelname} {record.getMessage()}"
            if record.exc_info and record.exc_info[1] is not None:
                line += f" ({type(record.exc_info[1]).__name__}: {record.exc_info[1]})"
            with self._lines_lock:
                self.lines.append(line[:600])
        except Exception:  # noqa: BLE001 - a log handler must never raise
            self.handleError(record)

    def snapshot(self) -> list[str]:
        with self._lines_lock:
            return list(self.lines)


def redact(text: str, names: list[str] | None = None) -> str:
    """Take device names and anything personal or secret out of text that will be public."""
    out = str(text or "")
    out = _SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}[hidden]", out)
    out = _BEARER.sub("Bearer [hidden]", out)
    out = _EMAIL.sub("[email]", out)
    out = _PERSONAL.sub(lambda m: f"{m.group(1)}{m.group(2)}[hidden]", out)
    out = _IEEE.sub("[ieee]", out)
    unique = sorted({str(n).strip() for n in names or [] if len(str(n or "").strip()) >= 3}, key=len, reverse=True)
    for number, name in enumerate(unique, 1):
        out = re.sub(rf"(?<![\w]){re.escape(name)}(?![\w])", f"[device {number}]", out, flags=re.IGNORECASE)
    return out


def _clean(value: Any, limit: int = TEXT_MAX) -> str:
    return str(value or "").replace("\r\n", "\n").strip()[:limit]


def setup_lines(env: dict) -> list[str]:
    """The setup block, as Markdown list items."""
    lines = [f"- Z2M Device Tracker {env.get('app_version') or '?'} ({env.get('mode') or '?'})"]
    ha = env.get("ha_version") or "?"
    install = env.get("install") or ""
    lines.append(f"- Home Assistant {ha}" + (f" on {install}" if install else ""))
    if env.get("arch"):
        lines.append(f"- Architecture: {env['arch']}")
    for net in env.get("networks") or []:
        parts = [net.get("label") or net.get("source") or "?"]
        if net.get("version"):
            parts[0] += f" {net['version']}"
        if net.get("coordinator"):
            parts.append(f"{net['coordinator']} adapter")
        count = int(net.get("devices") or 0)
        parts.append(f"{count} device" + ("" if count == 1 else "s"))
        lines.append(f"- {', '.join(parts)}")
    if not env.get("networks"):
        lines.append("- No networks switched on")
    if env.get("broker"):
        lines.append(f"- MQTT broker: {env['broker']}")
    if env.get("integration") is not None:
        lines.append(f"- Integration: {'connected' if env['integration'] else 'not connected'}")
    extra = [x for x in (env.get("language") and f"language {env['language']}", env.get("browser")) if x]
    if extra:
        lines.append(f"- Sidebar: {' · '.join(extra)}")
    return lines


def _z2m_versions(env: dict) -> str:
    nets = [n for n in env.get("networks") or [] if n.get("source") == "z2m"]
    if not nets:
        others = ", ".join(n.get("label") or n.get("source") for n in env.get("networks") or [])
        return f"Not used ({others})" if others else "Not used"
    versions = ", ".join(n.get("version") or "?" for n in nets)
    return versions if len(nets) == 1 else f"{versions} ({len(nets)} instances)"


def build(
    kind: str,
    form: dict,
    env: dict,
    logs: list[str],
    names: list[str],
    include_setup: bool = True,
    include_logs: bool = True,
) -> dict:
    """The issues/new link for one report, plus what had to give to keep it short enough.

    Returns ``url`` ("" while required fields are empty), ``markdown`` (the whole report, for
    copying when the link can't carry it), ``missing`` (required field ids still empty),
    ``over`` (characters past GitHub's limit), and how many log lines fit.
    """
    kind = kind if kind in KINDS else "bug"
    template, prefix, required, optional = KINDS[kind]
    form = form if isinstance(form, dict) else {}
    text = {
        key: redact(_clean(form.get(key), TITLE_MAX if key == "title" else TEXT_MAX), names)
        for key in required + optional
    }
    missing = [key for key in required if not text[key]]
    setup = "\n".join(setup_lines(env)) if include_setup else ""
    log_lines = [redact(line, names) for line in logs] if include_logs and kind == "bug" else []

    fields: dict[str, str] = {}
    if kind == "bug":
        fields["what_happened"] = text["what_happened"]
        fields["expected"] = text["expected"]
        fields["steps"] = text["steps"]
        if include_setup:
            fields["app_version"] = str(env.get("app_version") or "")
            fields["ha_version"] = str(env.get("ha_version") or "")
            fields["z2m_version"] = _z2m_versions(env)
            fields["broker"] = str(env.get("broker") or "")
    else:
        fields["problem"] = text["problem"]
        fields["solution"] = text["solution"]
        fields["alternatives"] = text["alternatives"]
    fields["setup"] = setup

    def link(keep: int) -> str:
        query = {"template": template, "title": prefix + text["title"]}
        query.update({k: v for k, v in fields.items() if v})
        if keep:
            query["logs"] = "\n".join(log_lines[-keep:])
        return f"{ISSUE_URL}?{urlencode(query)}"

    keep = len(log_lines)
    url = link(keep)
    while len(url) > URL_MAX and keep > 0:
        keep -= 1
        url = link(keep)
    over = max(0, len(url) - URL_MAX)

    return {
        "url": "" if missing or over else url,
        "title": prefix + text["title"],
        "markdown": markdown(kind, text, setup, log_lines),
        "setup": setup,
        "logs": log_lines,
        "missing": missing,
        "over": over,
        "logs_used": keep,
        "logs_dropped": len(log_lines) - keep,
        "search_url": f"{ISSUES_URL}?{urlencode({'q': ('is:issue ' + text['title']).strip()})}",
    }


def markdown(kind: str, text: dict, setup: str, logs: list[str]) -> str:
    """The whole report as Markdown, for pasting when the link would be too long."""
    _, prefix, _, _ = KINDS[kind]

    def section(heading: str, body: str, fence: str = "") -> str:
        if not body:
            return ""
        return f"### {heading}\n\n" + (f"```{fence}\n{body}\n```" if fence else body) + "\n"

    if kind == "bug":
        parts = [
            section("What happened?", text["what_happened"]),
            section("What did you expect to happen?", text["expected"]),
            section("Steps to reproduce", text["steps"]),
            section("Setup", setup),
            section("App log", "\n".join(logs), "shell"),
        ]
    else:
        parts = [
            section("What are you trying to do?", text["problem"]),
            section("What would you like the tracker to do?", text["solution"]),
            section("What have you tried instead?", text["alternatives"]),
            section("Setup", setup),
        ]
    return "\n".join([f"## {prefix}{text['title']}\n", *[p for p in parts if p]])
