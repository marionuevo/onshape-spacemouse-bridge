"""Per-connection WAMP handshake and the property read/write interface to
the page (Controller). Mirrors 3dconnexion.js's create/update/delete and
subscribe sequence -- see wamp.py's module docstring for the captured
handshake this implements.
"""
from __future__ import annotations

import asyncio
import logging
import random
import string
from typing import Awaitable, Callable, Optional

from . import navlib, wamp

_RES_MOUSE = "3dconnexion:3dmouse"
_RES_CONTROLLER = "3dconnexion:3dcontroller"

log = logging.getLogger("bridge")


def _short_id(n: int = 10) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


class Controller:
    """The live property interface to one page's 3dcontroller instance.

    The driver holds no persistent view state: it seeds from the app on
    every read. Keep this per-connection and let it die with the socket --
    that is what makes a page refresh correct for free, instead of snapping
    the camera back to a stale cached pose.
    """

    def __init__(self, session: wamp.Session, topic: str, instance_id: str, info: navlib.ClientInfo):
        self.session = session
        self.topic = topic
        self.id = instance_id
        self.info = info
        self.quirks = navlib.quirks_for(info)

        self.focus = False
        self.client_drives_frames = False
        self._frame_times: asyncio.Queue = asyncio.Queue(maxsize=1)

    def set_focus(self, v: bool) -> None:
        self.focus = v

    def set_client_drives_frames(self, v: bool) -> None:
        self.client_drives_frames = v

    def notify_frame_time(self, t: float) -> None:
        """Publish a client animation timestamp without ever blocking; only
        the most recent value matters.
        """
        while not self._frame_times.empty():
            try:
                self._frame_times.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            self._frame_times.put_nowait(t)
        except asyncio.QueueFull:
            pass

    async def next_frame_time(self, timeout: Optional[float] = None) -> Optional[float]:
        try:
            return await asyncio.wait_for(self._frame_times.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    # --- Property access --------------------------------------------------

    async def read(self, prop: str):
        return await self.session.call_client(self.topic, navlib.PROC_READ, prop)

    async def update(self, prop: str, value) -> None:
        await self.session.call_client(self.topic, navlib.PROC_UPDATE, prop, value)

    async def read_bool(self, prop: str) -> bool:
        return bool(await self.read(prop))

    async def read_float(self, prop: str) -> float:
        return float(await self.read(prop))

    async def read_vec3(self, prop: str) -> navlib.Vec3:
        v = await self.read(prop)
        return (float(v[0]), float(v[1]), float(v[2]))

    async def read_box(self, prop: str) -> navlib.Box:
        v = await self.read(prop)
        return tuple(float(x) for x in v)

    async def read_matrix4(self, prop: str) -> navlib.Mat4:
        v = await self.read(prop)
        return navlib.canonical(v, self.quirks.layout)

    async def write_matrix4(self, prop: str, m: navlib.Mat4) -> None:
        await self.update(prop, navlib.from_canonical(m, self.quirks.layout))

    async def write_box(self, prop: str, b: navlib.Box) -> None:
        await self.update(prop, list(b))

    async def set_motion(self, moving: bool) -> None:
        await self.update(navlib.PROP_MOTION, moving)

    async def begin_transaction(self, n: int) -> None:
        await self.update(navlib.PROP_TRANSACTION, n)

    async def end_transaction(self) -> None:
        await self.update(navlib.PROP_TRANSACTION, 0)

    # --- Fire-and-forget writes for the per-frame hot path -----------------
    #
    # drive.py's loop used to await set_motion/begin_transaction/write_*/
    # end_transaction in strict sequence -- five WAMP round trips, one after
    # another, on every single frame. On Python/asyncio that overhead (JSON
    # encode, a TLS record, event-loop scheduling) is small per message but
    # adds up against a ~16ms frame budget in a way it wouldn't in a
    # compiled native driver. These variants send the same messages without
    # waiting for an acknowledgement; see wamp.Session.cast_client for why
    # that's still safe with respect to message ordering.

    async def cast(self, prop: str, value) -> None:
        await self.session.cast_client(self.topic, navlib.PROC_UPDATE, prop, value)

    async def cast_motion(self, moving: bool) -> None:
        await self.cast(navlib.PROP_MOTION, moving)

    async def cast_transaction(self, n: int) -> None:
        await self.cast(navlib.PROP_TRANSACTION, n)

    async def cast_matrix4(self, prop: str, m: navlib.Mat4) -> None:
        await self.cast(prop, navlib.from_canonical(m, self.quirks.layout))

    async def cast_box(self, prop: str, b: navlib.Box) -> None:
        await self.cast(prop, list(b))


OnReady = Callable[[Controller], Awaitable[None]]
OnRelease = Callable[[], Awaitable[None]]
OnFrameTime = Callable[[Controller, float], None]
OnClientInfo = Callable[[navlib.ClientInfo, navlib.Quirks], None]


class Bridge:
    """Implements the wamp.Handler protocol for one page connection: runs
    the 3dmouse/3dcontroller handshake and surfaces a ready Controller via
    on_ready once the client subscribes to its controller topic.
    """

    def __init__(
        self,
        on_ready: Optional[OnReady] = None,
        on_frame_time: Optional[OnFrameTime] = None,
        on_client_info: Optional[OnClientInfo] = None,
        on_release: Optional[OnRelease] = None,
    ):
        self.on_ready = on_ready
        self.on_frame_time = on_frame_time
        self.on_client_info = on_client_info
        self.on_release = on_release

        self._connexion_id: Optional[str] = None
        self._instance_id: Optional[str] = None
        self._info: Optional[navlib.ClientInfo] = None
        self.controller: Optional[Controller] = None
        # An update carrying focus or frame.timingSource can in principle
        # arrive before the SUBSCRIBE that creates the Controller. The
        # captured Onshape handshake subscribes first, but dropping those on
        # the floor if a client ever reorders them is a silent total
        # failure: focus stays False and the drive loop skips every tick
        # with the device looking dead. Latch them and apply on subscribe.
        self._pending_focus: Optional[bool] = None
        self._pending_timing_source: Optional[bool] = None

    async def on_call(self, session: wamp.Session, proc_uri: str, args: list):
        # proc_uri arrives resolved, e.g. "wss://127.51.68.120/3dconnexion#create".
        op = proc_uri.rsplit("#", 1)[-1]
        if op == "create":
            return await self._handle_create(args)
        if op == "update":
            return await self._handle_update(args)
        if op == "delete":
            return await self._handle_delete()
        raise ValueError(f"unknown procedure {proc_uri!r}")

    async def _handle_create(self, args: list):
        if not args:
            raise ValueError("create: missing resource")
        resource = args[0]

        if resource == _RES_MOUSE:
            lib_version = args[1] if len(args) > 1 else ""
            self._connexion_id = "mouse-" + _short_id()
            log.info("3dmouse created connexion=%s clientLibVersion=%s", self._connexion_id, lib_version)
            return {"connexion": self._connexion_id}

        if resource == _RES_CONTROLLER:
            if len(args) < 3:
                raise ValueError("create 3dcontroller: want connexion and info")
            connexion, info_raw = args[1], args[2]
            if connexion != self._connexion_id:
                raise ValueError(f"create 3dcontroller: unknown connexion {connexion!r}")
            if not isinstance(info_raw, dict):
                raise ValueError("create 3dcontroller: bad info")

            info = navlib.ClientInfo(
                name=info_raw.get("name", ""),
                version=navlib.parse_version(info_raw.get("version")),
                row_major_order=info_raw.get("rowMajorOrder"),
            )
            self._instance_id = "ctl-" + _short_id()
            self._info = info
            q = navlib.quirks_for(info)
            if info.version == navlib.UNKNOWN_VERSION:
                log.warning(
                    "client %r reported an unreadable version %r; assuming %s matrices",
                    info.name, info_raw.get("version"), q.layout,
                )
            log.info(
                "3dcontroller created instance=%s client=%s clientVersion=%s layout=%s frameTiming=%s",
                self._instance_id, info.name, info.version, q.layout, q.frame_timing,
            )
            known_version = info.version != navlib.UNKNOWN_VERSION
            if info.row_major_order is None and known_version and info.version < 0.5:
                log.warning("client %r predates 3DconnexionJS 0.5; assuming row-major matrices", info.name)
            if self.on_client_info:
                self.on_client_info(info, q)
            return {"instance": self._instance_id}

        raise ValueError(f"create: unknown resource {resource!r}")

    async def _handle_update(self, args: list):
        if len(args) < 2 or not isinstance(args[1], dict):
            return {}
        payload = args[1]
        c = self.controller

        if "focus" in payload:
            focus = bool(payload["focus"])
            if c is not None:
                c.set_focus(focus)
            else:
                self._pending_focus = focus
            log.debug("client focus=%s", payload["focus"])

        frame = payload.get("frame")
        if isinstance(frame, dict):
            if "timingSource" in frame:
                on = bool(frame["timingSource"])
                if c is not None:
                    c.set_client_drives_frames(on)
                else:
                    self._pending_timing_source = on
                log.info("client frame timing clientDriven=%s", on)
            if "time" in frame and c is not None:
                # Must return promptly: the page is waiting on this reply
                # with roughly a 60ms budget before it abandons its
                # animation loop. c.notify_frame_time never blocks.
                t = float(frame["time"])
                c.notify_frame_time(t)
                if self.on_frame_time:
                    self.on_frame_time(c, t)

        return {}

    async def _handle_delete(self):
        """Release everything the handshake set up.

        Clearing only `self.controller` is not enough: `_instance_id` would
        survive, so a later SUBSCRIBE passes both guards in `on_subscribe`
        and builds a *second* Controller while the drive loop for the first
        is still running -- two loops writing view.affine on different
        topics, the orphan keeping the focus it was last told about because
        subsequent updates only ever reach the current controller.
        """
        log.info("client released the 3dmouse")
        self.controller = None
        self._connexion_id = None
        self._instance_id = None
        self._info = None
        self._pending_focus = None
        self._pending_timing_source = None
        if self.on_release:
            await self.on_release()
        return {}

    async def on_subscribe(self, session: wamp.Session, topic: str) -> None:
        if self._instance_id is None:
            log.warning("subscribe before 3dcontroller was created topic=%s", topic)
            return
        if self.controller is not None:
            return
        c = Controller(session, topic, self._instance_id, self._info)
        if self._pending_focus is not None:
            c.set_focus(self._pending_focus)
        if self._pending_timing_source is not None:
            c.set_client_drives_frames(self._pending_timing_source)
        self._pending_focus = None
        self._pending_timing_source = None
        self.controller = c
        log.info("controller subscribed topic=%s", topic)
        if self.on_ready:
            await self.on_ready(c)
