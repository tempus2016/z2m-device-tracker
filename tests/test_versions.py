"""Every place that states the version must agree with the app's config.yaml.

The app's log line, HTTP server header, /api/version and MQTT discovery read VERSION
from main.py; the card logs its own constant. Both were left at 2.0.0 through the
2.0.1 and 2.0.2 releases, so a bump that misses one now fails here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


def _config_version() -> str:
    return str(yaml.safe_load((ROOT / "z2m_tracker/config.yaml").read_text(encoding="utf-8"))["version"])


def _constant(path: str, pattern: str) -> str:
    match = re.search(pattern, (ROOT / path).read_text(encoding="utf-8"), re.MULTILINE)
    assert match, f"no version constant found in {path}"
    return match.group(1)


SOURCES = {
    "app main.py VERSION": lambda: _constant("z2m_tracker/app/main.py", r'^VERSION = "([^"]+)"$'),
    "integration const.py VERSION": lambda: _constant(
        "custom_components/z2m_tracker/const.py", r'^VERSION = "([^"]+)"$'
    ),
    "integration manifest.json": lambda: json.loads(
        (ROOT / "custom_components/z2m_tracker/manifest.json").read_text(encoding="utf-8")
    )["version"],
    "card Z2M_TRACKER_CARD_VERSION": lambda: _constant(
        "custom_components/z2m_tracker/www/z2m-tracker-card.js", r'^const Z2M_TRACKER_CARD_VERSION = "([^"]+)";$'
    ),
}


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_version_matches_config(name: str) -> None:
    assert SOURCES[name]() == _config_version(), f"{name} does not match z2m_tracker/config.yaml"
