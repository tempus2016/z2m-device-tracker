"""battery_library.BatteryLibrary lookups against a small made-up library."""

from __future__ import annotations

from pathlib import Path

import pytest
from battery_library import BatteryLibrary, _norm
from conftest import FIXTURES

APP_LIBRARY = Path(__file__).parent.parent / "z2m_tracker" / "app" / "battery_library.json"


@pytest.fixture(scope="module")
def lib() -> BatteryLibrary:
    return BatteryLibrary(FIXTURES / "battery_library.json")


def test_norm():
    assert _norm("Acme Ltd.") == "acmeltd"
    assert _norm(None) == ""
    assert _norm("MS-9") == "ms9"


def test_counts_every_row(lib):
    assert lib.count == 5


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        # Code in brackets at the end of the model text, matched on vendor + Z2M model.
        ({"vendor": "Acme", "model": "DS-01"}, ("CR2032", 1)),
        # Model id column, matched on manufacturer + model_id, ignoring case and punctuation.
        ({"manufacturer": "ACME", "model_id": "ms9"}, ("AAA", 2)),
        # Vendor + description text.
        ({"vendor": "Acme", "description": "Motion sensor"}, ("AAA", 2)),
        # Unknown vendor, but the model code only appears once in the library.
        ({"vendor": "Someone else", "model": "P1"}, ("None", 1)),
        # Quantity 0 is read as 1.
        ({"vendor": "Widgetry", "model": "P1"}, ("None", 1)),
        # Vendor narrows an otherwise ambiguous code.
        ({"vendor": "Bar", "model": "R1"}, ("CR2450", 1)),
    ],
)
def test_lookup_matches(lib, record, expected):
    assert lib.lookup(record) == expected


@pytest.mark.parametrize(
    "record",
    [
        {"vendor": "Baz", "model": "R1"},  # two different batteries share this code
        {"vendor": "Acme", "model": "nothing"},
        {},
    ],
)
def test_lookup_misses(lib, record):
    assert lib.lookup(record) is None


@pytest.mark.parametrize("content", [None, "not json"])
def test_missing_or_broken_file(tmp_path, content):
    path = tmp_path / "library.json"
    if content is not None:
        path.write_text(content)
    empty = BatteryLibrary(path)
    assert empty.count == 0
    assert empty.lookup({"vendor": "Acme", "model": "DS-01"}) is None


def test_bundled_library_loads():
    bundled = BatteryLibrary(APP_LIBRARY)
    assert bundled.count > 1000
