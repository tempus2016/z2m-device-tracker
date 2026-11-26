"""Base topics from the app options keep their spaces (issue #33)."""

from __future__ import annotations

from tracker_base import base_topic_list


def test_trailing_space_is_kept():
    assert base_topic_list(["Z2MQTT "]) == ["Z2MQTT "]


def test_leading_space_is_kept():
    assert base_topic_list([" z2m"]) == [" z2m"]


def test_outer_slashes_are_dropped():
    assert base_topic_list(["/zigbee2mqtt/", "home/z2m/"]) == ["zigbee2mqtt", "home/z2m"]


def test_blank_and_repeated_entries_are_dropped():
    assert base_topic_list(["", "   ", "/", "zigbee2mqtt", "zigbee2mqtt", 5]) == ["zigbee2mqtt"]


def test_spaced_and_plain_topics_are_different():
    assert base_topic_list(["Z2MQTT ", "Z2MQTT"]) == ["Z2MQTT ", "Z2MQTT"]


def test_default_when_empty_or_missing():
    assert base_topic_list([]) == ["zigbee2mqtt"]
    assert base_topic_list(None) == ["zigbee2mqtt"]
