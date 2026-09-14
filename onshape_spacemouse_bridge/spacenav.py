"""Reads 6-DoF input from the spacenavd daemon over its native libspnav
AF_UNIX protocol.

We build on spacenavd rather than reading evdev directly because it already
handles device grab (Xorg/Wayland input stacks will otherwise also treat the
puck as a pointer), hotplug, multi-client arbitration (Blender and this
process can hold the device at once), and per-axis deadzone/sensitivity via
/etc/spnavrc.
"""
from __future__ import annotations

import socket
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional, Union

SOCKET_PATHS = ["/var/run/spnav.sock", "/run/spnav.sock"]

_EV_MOTION = 0
_EV_BUTTON_PRESS = 1
_EV_BUTTON_RELEASE = 2

_EVENT_SIZE = 32  # 8 x int32
_EVENT_QUEUE = 256  # events retained between poll_events() calls
_STRUCT = struct.Struct("<8i")


@dataclass
class Motion:
    """A 6-DoF deflection, in raw device units as filtered by spacenavd
    (deadzone and sensitivity already applied per /etc/spnavrc).

    Axes are a Z-up right-handed frame, verified with -calibrate on a
    SpaceMouse Compact:

        +X  cap slides RIGHT
        +Y  cap slides AWAY from the user, toward the screen
        +Z  cap lifts UP
        +RX right-handed about X: far edge LIFTS (tipping forward is negative)
        +RY right-handed about Y: right edge goes DOWN
        +RZ right-handed about Z: COUNTER-clockwise seen from above
    """

    x: int = 0
    y: int = 0
    z: int = 0
    rx: int = 0
    ry: int = 0
    rz: int = 0
    period_ms: int = 0

    def is_zero(self) -> bool:
        return not any((self.x, self.y, self.z, self.rx, self.ry, self.rz))


@dataclass
class Button:
    id: int
    pressed: bool


Event = Union[Motion, Button]


def _decode(frame: tuple) -> Optional[Event]:
    """frame is 8 raw int32s straight off the wire. The slot order is
    (x, z, y) for BOTH translation and rotation -- applying the swap to only
    one silently exchanges yaw with roll. Confirmed by calibration: "twist
    clockwise" landed on the axis labelled RY and "tip right" on RZ.
    """
    kind = frame[0]
    if kind == _EV_MOTION:
        return Motion(
            x=frame[1], z=frame[2], y=frame[3],
            rx=frame[4], rz=frame[5], ry=frame[6],
            period_ms=frame[7],
        )
    if kind == _EV_BUTTON_PRESS:
        return Button(id=frame[1], pressed=True)
    if kind == _EV_BUTTON_RELEASE:
        return Button(id=frame[1], pressed=False)
    return None


class Client:
    """A connection to spacenavd.

    spacenavd streams motion at ~125 Hz while the puck is deflected -- far
    above display rate -- so navigation should be driven from `state()` on
    your own frame timer rather than one redraw per event. `poll_events()` is
    for buttons and for noticing the return to centre.

    A background thread does the blocking socket read and latches state;
    everything here is safe to call from an asyncio event loop thread.
    """

    def __init__(self, path: Optional[str] = None):
        candidates = [path] if path else SOCKET_PATHS
        sock = None
        last_err: Optional[OSError] = None
        chosen = None
        for p in candidates:
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(p)
                sock, chosen = s, p
                break
            except OSError as e:
                s.close()
                last_err = e
        if sock is None:
            raise RuntimeError(
                f"could not connect to spacenavd (tried {candidates}); "
                f"is spacenavd installed and running? {last_err}"
            )

        self._sock = sock
        self.socket_path = chosen

        self._lock = threading.Lock()
        self._latest = Motion()
        self._latest_at = 0.0
        # A bounded deque, not a list: at ~125Hz motion dominates the queue,
        # and dropping the oldest from a list is O(n) on every overflow.
        # Dropping the oldest (rather than refusing the newest) matters -- a
        # consumer watching for a state change such as the return to centre
        # would otherwise be served a stale backlog while the event it wants
        # is thrown away.
        self._events: deque[Event] = deque(maxlen=_EVENT_QUEUE)
        self._dead = threading.Event()
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _read_loop(self) -> None:
        buf = bytearray()
        try:
            while not self._closed.is_set():
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while len(buf) >= _EVENT_SIZE:
                    frame = _STRUCT.unpack(bytes(buf[:_EVENT_SIZE]))
                    del buf[:_EVENT_SIZE]
                    ev = _decode(frame)
                    if ev is None:
                        continue
                    with self._lock:
                        if isinstance(ev, Motion):
                            self._latest = ev
                            self._latest_at = time.monotonic()
                        self._events.append(ev)
        except OSError:
            pass
        finally:
            self._dead.set()

    # A held-and-released gesture can end without spacenavd ever reporting a
    # final "back to zero" sample -- observed in practice: a fast release can
    # cross from clearly-deflected to at-rest between two polls with nothing
    # in between, and depending on the device/daemon version the daemon does
    # not always keep re-sending an unchanged resting value the way it does
    # for a *held* off-centre position. Without this, `state()` would return
    # the last real deflection forever, and the camera would keep moving
    # until a fresh gesture happened to overwrite it -- exactly "it keeps
    # spinning until you bring the mouse back to centre yourself".
    #
    # motion is expected at ~125Hz while anything is happening (see the
    # spacenavd notes in the project README), so anything gone quiet this
    # much longer than one period is not a device still being held.
    _STALE_AFTER = 0.15  # seconds

    def state(self) -> Motion:
        """The most recent deflection, or a centred Motion if nothing has
        been heard from spacenavd recently (see _STALE_AFTER above). Safe to
        poll on a timer; that's the intended use -- see poll_events for
        buttons and edge-triggered events instead.
        """
        with self._lock:
            if self._latest_at and (time.monotonic() - self._latest_at) > self._STALE_AFTER:
                return Motion()
            return self._latest

    def poll_events(self) -> list[Event]:
        """Drain and return queued events (Motion and Button) since the last
        call. Buttons live here; do not rely on this for motion timing.
        """
        with self._lock:
            evs, self._events = self._events, deque(maxlen=_EVENT_QUEUE)
        return list(evs)

    def is_dead(self) -> bool:
        return self._dead.is_set()

    def close(self) -> None:
        self._closed.set()
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()
