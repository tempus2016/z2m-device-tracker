"""battery_library.clean_shop_url: the battery shop link setting."""

from __future__ import annotations

import pytest
from battery_library import SHOP_URL_MAX, clean_shop_url


@pytest.mark.parametrize("value", [None, "", "   "])
def test_empty_turns_it_off(value):
    assert clean_shop_url(value) == ""


@pytest.mark.parametrize(
    "value",
    [
        "https://www.amazon.co.uk/s?k={type}+battery",
        "https://www.amazon.de/s?k={type}",
        "http://shop.example/search?q={type}&cat=batteries",
    ],
)
def test_store_urls_kept(value):
    assert clean_shop_url(f"  {value} ") == value


@pytest.mark.parametrize(
    "value",
    [
        "javascript:alert(1)//{type}",
        "data:text/html,{type}",
        "www.amazon.co.uk/s?k={type}",
        "https://www.amazon.co.uk/s?k=CR2032",
        "https://shop.example/s?k={type} battery",
        'https://shop.example/s?k={type}"onmouseover="x',
        "https://shop.example/<b>{type}",
        "https:///{type}",
        "https://shop.example/" + "x" * SHOP_URL_MAX + "{type}",
        42,
    ],
)
def test_bad_urls_rejected(value):
    with pytest.raises(ValueError):
        clean_shop_url(value)
