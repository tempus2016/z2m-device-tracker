"""Every translation file against its English source.

Covers the sidebar and cards (z2m_tracker/app/www/i18n), the app's options
(z2m_tracker/translations) and the integration (custom_components/z2m_tracker/translations).
A missing key is allowed (it falls back to English). Unknown keys, changed placeholders,
and broken plural forms are not.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SIDEBAR = ROOT / "z2m_tracker/app/www/i18n"
OPTIONS = ROOT / "z2m_tracker/translations"
INTEGRATION = ROOT / "custom_components/z2m_tracker/translations"
CARDS_JS = ROOT / "custom_components/z2m_tracker/www/z2m-tracker-i18n.js"
LANG_RE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$")
PLACEHOLDER = re.compile(r"\{(\w+)\}")
PLURAL_FORMS = {"zero", "one", "two", "few", "many", "other"}


def load(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    data = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    assert isinstance(data, dict), f"{path.name}: top level is not an object"
    return data


def flatten(data: dict, prefix: str = "") -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            out.update(flatten(value, f"{prefix}{key}."))
        else:
            out[f"{prefix}{key}"] = value
    return out


def placeholders(value: object) -> set[str]:
    texts = value.values() if isinstance(value, dict) else [value]
    return {name for text in texts for name in PLACEHOLDER.findall(str(text))}


def problems(en: dict, other: dict, plurals: bool) -> list[str]:
    found = [f"{key}: not in English" for key in sorted(set(other) - set(en))]
    for key in sorted(set(other) & set(en)):
        want, got = en[key], other[key]
        if plurals and isinstance(want, dict):
            if not isinstance(got, dict):
                found.append(f"{key}: English has plural forms, this is a plain string")
                continue
            if "other" not in got:
                found.append(f'{key}: plural forms need "other"')
            if set(got) - PLURAL_FORMS:
                found.append(f"{key}: unknown plural forms {sorted(set(got) - PLURAL_FORMS)}")
        elif not isinstance(got, str):
            found.append(f"{key}: not a string")
            continue
        if placeholders(want) != placeholders(got):
            found.append(f"{key}: placeholders {sorted(placeholders(got))}, English has {sorted(placeholders(want))}")
    return found


def others(folder: Path, suffix: str) -> list[Path]:
    return sorted(p for p in folder.glob(f"*{suffix}") if p.stem != "en")


@pytest.mark.parametrize("path", others(SIDEBAR, ".json"), ids=lambda p: p.stem)
def test_sidebar(path: Path) -> None:
    assert LANG_RE.match(path.stem)
    data = load(path)
    assert data.get("language.name"), "set language.name to the language's own name"
    assert problems(load(SIDEBAR / "en.json"), data, plurals=True) == []


@pytest.mark.parametrize("path", others(OPTIONS, ".yaml"), ids=lambda p: p.stem)
def test_app_options(path: Path) -> None:
    assert problems(flatten(load(OPTIONS / "en.yaml")), flatten(load(path)), plurals=False) == []


@pytest.mark.parametrize("path", others(INTEGRATION, ".json"), ids=lambda p: p.stem)
def test_integration(path: Path) -> None:
    assert problems(flatten(load(INTEGRATION / "en.json")), flatten(load(path)), plurals=False) == []


def test_option_labels_cover_config() -> None:
    config = yaml.safe_load((ROOT / "z2m_tracker/config.yaml").read_text(encoding="utf-8"))
    assert set(load(OPTIONS / "en.yaml")["configuration"]) == set(config["schema"])


def test_card_english_matches_sidebar() -> None:
    """The cards' built-in English is a copy of the card.* keys in en.json."""
    en = load(SIDEBAR / "en.json")
    js = CARDS_JS.read_text(encoding="utf-8")
    built_in = set(re.findall(r'^\s+"(card\.[\w.]+)":', js, re.MULTILINE))
    assert built_in == {k for k in en if k.startswith("card.")}
