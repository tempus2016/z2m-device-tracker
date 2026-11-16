"""MQTT connection problems: plain-language explanations, log throttling and the HA notification."""

from __future__ import annotations

import socket

import mqtt_status
from mqtt_status import NO_BROKER, NOTIFY_AFTER, RELOG_AFTER, MqttStatus, explain_error, explain_refusal, probe


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make(configured: bool = True) -> tuple[MqttStatus, Clock]:
    clock = Clock()
    return MqttStatus("core-mosquitto", 1883, configured=configured, clock=clock), clock


# ------------------------------------------------------------ explanations


def test_unknown_host_points_at_mosquitto_app():
    text = explain_error(socket.gaierror(-2, "Name or service not known"), "core-mosquitto", 1883)
    assert "Can't find the MQTT broker 'core-mosquitto'" in text
    assert "Mosquitto broker app" in text
    # The broker was set up when the app started, so it reconnects on its own: no restart needed.
    assert "restart" not in text.lower()
    assert "reconnects by itself" in text


def test_refused_and_timeout_name_the_address():
    assert "core-mosquitto:1883" in explain_error(ConnectionRefusedError(), "core-mosquitto", 1883)
    assert "No answer" in explain_error(TimeoutError(), "core-mosquitto", 1883)


def test_broker_answered_but_mqtt_failed():
    assert "answered" in explain_error(None, "core-mosquitto", 1883)


def test_other_errors_keep_the_detail():
    assert "boom" in explain_error(OSError("boom"), "core-mosquitto", 1883)


def test_login_refusal_says_restart():
    assert "Restart this app" in explain_refusal("Not authorized")
    assert "Restart this app" in explain_refusal("Bad user name or password")
    assert explain_refusal("Server unavailable").endswith("Server unavailable")


def test_manual_broker_points_at_the_options_not_the_app():
    for err in (socket.gaierror(-2, "Name or service not known"), ConnectionRefusedError(), None):
        text = explain_error(err, "192.168.1.5", 1883, manual=True)
        assert "Mosquitto broker app" not in text
        assert "configuration" in text or "mqtt_ssl" in text
    login = explain_refusal("Not authorized", manual=True)
    assert "mqtt_user and mqtt_password" in login
    assert "Restart this app" not in login


def test_no_broker_mentions_the_option():
    assert "mqtt_host" in NO_BROKER


def test_probe_reports_refused_port():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    port = server.getsockname()[1]
    server.close()
    assert isinstance(probe("127.0.0.1", port, timeout=1), ConnectionRefusedError)


def test_probe_none_when_something_listens():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    try:
        assert probe("127.0.0.1", server.getsockname()[1], timeout=1) is None
    finally:
        server.close()


# ------------------------------------------------------------ log throttling


def test_same_problem_logged_once_per_interval():
    status, clock = make()
    assert status.failed("broker gone") == "broker gone"
    clock.now += 30
    assert status.failed("broker gone") is None
    clock.now += RELOG_AFTER
    assert status.failed("broker gone") == "broker gone"


def test_new_problem_logged_straight_away():
    status, _ = make()
    status.failed("broker gone")
    assert status.failed("login refused") == "login refused"


def test_no_broker_overrides_the_network_error():
    status, _ = make(configured=False)
    assert status.failed("") == NO_BROKER
    assert status.failed("Can't find the MQTT broker 'core-mosquitto'.") is None


def test_connecting_resets_the_log():
    status, _ = make()
    status.failed("broker gone")
    status.up()
    assert status.failed("broker gone") == "broker gone"


# ------------------------------------------------------------ notification


def test_notification_waits_then_fires_once():
    status, clock = make()
    status.failed("broker gone")
    assert status.notification_due() is None
    clock.now += NOTIFY_AFTER
    message = status.notification_due()
    assert message.startswith("broker gone")
    assert "keeps retrying" in message
    assert status.notification_due() is None


def test_no_notification_while_connected():
    status, clock = make()
    status.up()
    clock.now += NOTIFY_AFTER * 10
    assert status.notification_due() is None


def test_first_connect_dismisses_leftover_notification():
    status, _ = make()
    assert status.up() is True
    assert status.up() is False


def test_reconnect_asks_to_dismiss_only_after_notifying():
    status, clock = make()
    status.up()
    assert status.up() is False
    status.lost()
    clock.now += NOTIFY_AFTER
    assert status.notification_due()
    assert status.up() is True


def test_drop_restarts_the_wait():
    status, clock = make()
    status.up()
    clock.now += NOTIFY_AFTER * 5
    status.lost()
    assert status.notification_due() is None
    clock.now += NOTIFY_AFTER
    assert "MQTT broker" in status.notification_due()


def test_no_broker_message_in_notification():
    status, clock = make(configured=False)
    status.failed("")
    clock.now += NOTIFY_AFTER
    assert status.notification_due().startswith(mqtt_status.NO_BROKER)


def test_refusal_flag_set_and_cleared():
    status, _ = make()
    status.failed("login refused", refused=True)
    assert status.refused
    status.failed("broker gone")
    assert not status.refused
    status.failed("login refused", refused=True)
    status.up()
    assert not status.refused


def test_refusal_shown_even_without_broker_service():
    """Started before Mosquitto: no credentials, so the broker refuses. That beats "no broker"."""
    status, _ = make(configured=False)
    status.failed("")
    assert status.failed("login refused", refused=True) == "login refused"


def test_notification_updates_when_problem_changes():
    status, clock = make(configured=False)
    status.failed("")
    clock.now += NOTIFY_AFTER
    assert status.notification_due().startswith(NO_BROKER)
    status.failed("login refused", refused=True)
    assert status.notification_due().startswith("login refused")
    assert status.notification_due() is None
