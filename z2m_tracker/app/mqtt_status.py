"""Plain-language MQTT connection problems: what went wrong and what to do about it."""

from __future__ import annotations

import socket
import time
from collections.abc import Callable

# How long the broker can stay unreachable before Home Assistant gets a notification.
NOTIFY_AFTER = 120
# Repeat the same problem in the log at most this often.
RELOG_AFTER = 600

NOTIFICATION_ID = "z2m_tracker_mqtt"
NOTIFICATION_TITLE = "Zigbee Tracker can't reach MQTT"
NO_BROKER = (
    "No MQTT broker is set up in Home Assistant. Install and start the Mosquitto broker app, then restart this app."
)


def probe(host: str, port: int, timeout: float = 3.0) -> Exception | None:
    """Try a plain TCP connection to the broker; return the error, or None if it answered."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except Exception as err:  # noqa: BLE001 - every failure is explained to the user
        return err


def explain_error(err: Exception | None, host: str, port: int) -> str:
    """A network-level failure, in words a user can act on."""
    where = f"{host}:{port}"
    if isinstance(err, socket.gaierror):
        return (
            f"Can't find the MQTT broker '{host}'. Check the Mosquitto broker app is installed and started. "
            "This app reconnects by itself once it is."
        )
    if isinstance(err, ConnectionRefusedError):
        return f"Nothing is accepting connections at {where}. Check the Mosquitto broker app is started."
    if isinstance(err, (TimeoutError, socket.timeout)):
        return f"No answer from the MQTT broker at {where}. Check it is running and reachable."
    if err is None:
        return f"The MQTT broker at {where} answered, but the MQTT connection still failed. See the Mosquitto log."
    return f"Can't connect to the MQTT broker at {where}: {err}"


def explain_refusal(reason: str) -> str:
    """The broker answered but refused the connection (an MQTT reason code)."""
    text = reason.lower()
    if "not authorized" in text or "bad user name" in text or "password" in text:
        return (
            f"The MQTT broker refused this app's login ({reason}). Restart this app so it gets fresh "
            "credentials from the Mosquitto broker app."
        )
    return f"The MQTT broker refused the connection: {reason}"


class MqttStatus:
    """Tracks the broker connection and decides when to log or notify about a problem."""

    def __init__(self, host: str, port: int, configured: bool = True, clock: Callable[[], float] = time.monotonic):
        self.host = host
        self.port = port
        self.configured = configured
        self.connected = False
        self.problem: str | None = None
        self.refused = False
        self.notified = False
        self._notified_problem: str | None = None
        self._ever_connected = False
        self._clock = clock
        self._down_since = clock()
        self._logged: tuple[str, float] | None = None

    def failed(self, problem: str, refused: bool = False) -> str | None:
        """Record a failed attempt. Returns the line to log, or None if it was logged recently.

        `refused` marks a broker that answered and said no, which paho follows with a disconnect.
        """
        self.refused = refused
        if not self.configured and not refused:
            # A refusal proves a broker exists, so it says more than "no broker".
            problem = NO_BROKER
        if self.connected:
            self.connected = False
            self._down_since = self._clock()
        self.problem = problem
        now = self._clock()
        if self._logged and self._logged[0] == problem and now - self._logged[1] < RELOG_AFTER:
            return None
        self._logged = (problem, now)
        return problem

    def lost(self) -> None:
        """An established connection dropped."""
        if self.connected:
            self.connected = False
            self._down_since = self._clock()

    def up(self) -> bool:
        """Connected. Returns True if a notification may be showing and should be dismissed.

        True on the first connection too: one sent before a restart outlives the process that sent it.
        """
        first = not self._ever_connected
        self._ever_connected = True
        self.connected = True
        self.problem = None
        self.refused = False
        self._logged = None
        was_notified, self.notified = self.notified, False
        self._notified_problem = None
        return was_notified or first

    def notification_due(self) -> str | None:
        """The notification message once the broker has been unreachable for NOTIFY_AFTER seconds, else None.

        Sent again, replacing the first, when the problem changes.
        """
        if self.connected or self._clock() - self._down_since < NOTIFY_AFTER:
            return None
        problem = self.problem or f"No connection to the MQTT broker at {self.host}:{self.port} yet."
        if self.notified and problem == self._notified_problem:
            return None
        self.notified = True
        self._notified_problem = problem
        return f"{problem}\n\nThe tracker records nothing until it connects. It keeps retrying on its own."
