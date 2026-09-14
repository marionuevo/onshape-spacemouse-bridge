"""The live navigation loop: spacenav.Client + bridge.Controller + nav.Config
-> continuous camera updates. One instance runs per browser connection.

The device streams at ~125 Hz, far above display rate, so this samples its
latched state on its own timer rather than reacting to each event. A client
that sets frame.timingSource (Onshape 0.6.0 does) drives the clock instead;
our own ticker becomes a fallback used only to notice fresh input while
stopped.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Optional

from . import navlib, nav, wamp
from .bridge import Controller
from .spacenav import Button, Client as SpacenavClient

log = logging.getLogger("drive")

_EXTENTS_TTL = 2.0  # seconds -- re-reading model.extents every frame would
                     # double round trips for a value that rarely changes.
_SCENE_PROPS_TTL = 0.5  # seconds -- pivot/rotatable/perspective change rarely
                        # (switching to a flat sketch view, say); re-reading
                        # them every frame was most of the per-frame
                        # round-trip cost. view.affine itself can't be
                        # cached this way -- it also changes under the
                        # user's own mouse drag, not only under our writes.
_MAX_CLIENT_DT = 0.1  # seconds -- cap so a backgrounded tab can't teleport the camera
_MAX_EXTENTS_FAILURES = 3  # consecutive non-CALLERROR failures before giving
                           # up on model.extents for this connection
_MAX_CAMERA_FAILURES = 30  # consecutive failed view.affine reads before
                           # treating the client as gone -- 30 fast failures
                           # is ~half a second, 30 timeouts rather longer
_FIT_READ_TIMEOUT = 2.0  # a one-off button press, not a frame: wait properly
_FRAME_READ_TIMEOUT = 0.25  # seconds -- per-read deadline on the hot path
_CACHE_READ_TIMEOUT = 0.25  # seconds -- ditto for the between-burst refresh;
                            # wamp's 5s call timeout is a backstop for a dead
                            # connection, far too long to block a frame on


async def drive(
    device: SpacenavClient,
    controller: Controller,
    config: nav.Config,
    frame_rate: int = 60,
    buttons: Optional[dict[int, str]] = None,
) -> None:
    buttons = buttons or {}
    interval = 1.0 / frame_rate
    cfg = config  # local: button actions may toggle fields on this instance

    log.info(
        "navigation active client=%s frameRate=%d mode=%s dominantAxis=%s",
        controller.info.name, frame_rate, cfg.mode, cfg.dominant_axis,
    )

    moving = False
    frame = 0
    have_extents = False
    extents_unsupported = False
    extents_failures = 0
    camera_failures = 0
    extents: Optional[navlib.Box] = None
    extents_at = 0.0
    have_scene_props = False
    scene_props_at = 0.0
    cached_pivot: Optional[navlib.Vec3] = None
    cached_rotatable = True
    cached_perspective = True
    last = time.monotonic()
    last_client_t: Optional[float] = None

    async def stop_moving():
        nonlocal moving
        if not moving:
            return
        moving = False
        try:
            # Cast, not call: nothing here needs the page's acknowledgement,
            # and waiting for one cost a blocking round trip at the end of
            # every single gesture. It also deadlocked teardown -- this runs
            # from the loop's finally, which during a `delete` is reached
            # from the WebSocket read loop itself, the only thing that could
            # ever deliver the reply we would be waiting for.
            await controller.cast_motion(False)
        except Exception:  # noqa: BLE001
            # On cancellation the socket is already closing and send_str
            # raises ConnectionResetError. Anything escaping here would
            # surface only as an unretrieved task exception at every
            # disconnect, and a failed "stop" on a connection that is going
            # away regardless needs no handling.
            pass

    try:
        while True:
            got_client_frame, client_t = await _wait_for_tick(controller, interval, moving)
            # `last` must be refreshed on every iteration, whichever branch
            # supplied dt -- including the "skip this tick" case below.
            # Updating it only in the ticker branch (an earlier version of
            # this loop did exactly that) leaves it stale for as long as the
            # client keeps driving frames; the moment the ticker path is
            # ever taken again -- one dropped frame.time is enough -- dt
            # becomes "however long the client had been driving frames",
            # not one tick, and the camera jumps by that much in a single
            # step. This is what a Go port of the same loop gets for free:
            # there, `last = time.Now()` runs unconditionally after the
            # select, not inside one of its cases.
            now = time.monotonic()

            if got_client_frame:
                if last_client_t is not None and client_t > last_client_t:
                    dt = min((client_t - last_client_t) / 1000.0, _MAX_CLIENT_DT)
                else:
                    dt = interval
                last_client_t = client_t
            else:
                if controller.client_drives_frames and moving:
                    # Client supplies frames; our ticker would double-step.
                    # It still runs while stopped, to notice fresh input.
                    last = now
                    continue
                dt = min(now - last, _MAX_CLIENT_DT)

            last = now

            await _handle_buttons(device, controller, cfg, buttons)

            # The page tells us when its canvas has focus; respect it or we
            # fight whatever else the user is doing.
            if not controller.focus:
                await stop_moving()
                continue

            m = device.state()
            shaped = cfg.shape(m)
            if cfg.shape_is_zero(m, shaped):
                await stop_moving()
                continue

            now_m = time.monotonic()
            # Gated on `not moving`: refreshing a cache mid-gesture is what
            # was causing the occasional jump. If model.extents or the pivot
            # change between one held-burst frame and the next -- Onshape
            # recomputing a visible-bounds box, say -- rotating about a
            # pivot that just moved, or suddenly scaling translation speed
            # by a different model diagonal, reads as the camera randomly
            # leaping. A burst now sees a consistent scene end to end; these
            # only refresh in the gap between bursts, same as the real
            # driver treats view.affine itself (see navlib.py).
            #
            # The due refreshes are issued as one batch rather than awaited
            # one after another: they are independent properties, each
            # carries its own WAMP call id, and the page answers them
            # independently, so four sequential round trips cost four times
            # the latency of one for no benefit.
            want_extents = not extents_unsupported and (
                not have_extents or (not moving and (now_m - extents_at) > _EXTENTS_TTL)
            )
            want_scene_props = not have_scene_props or (
                not moving and (now_m - scene_props_at) > _SCENE_PROPS_TTL
            )
            if want_extents or want_scene_props:
                reads = {}
                if want_extents:
                    reads["extents"] = controller.read_box(navlib.PROP_MODEL_EXTENTS)
                if want_scene_props:
                    reads["pivot"] = controller.read_vec3(navlib.PROP_PIVOT_POSITION)
                    reads["rotatable"] = controller.read_bool(navlib.PROP_VIEW_ROTATABLE)
                    reads["perspective"] = controller.read_bool(navlib.PROP_VIEW_PERSPECTIVE)
                got = await _gather(reads, _CACHE_READ_TIMEOUT)

                if want_scene_props:
                    if not isinstance(got["pivot"], BaseException):
                        cached_pivot = got["pivot"]
                    if not isinstance(got["rotatable"], BaseException):
                        cached_rotatable = got["rotatable"]
                    if not isinstance(got["perspective"], BaseException):
                        cached_perspective = got["perspective"]
                    have_scene_props = True
                    scene_props_at = now_m

                if want_extents:
                    v = got["extents"]
                    if not isinstance(v, BaseException):
                        extents = v
                        have_extents = True
                        extents_failures = 0
                    else:
                        # Stop asking eventually. Leaving have_extents False
                        # kept `not have_extents` true, so the read was
                        # reissued every frame regardless of the TTL: a
                        # guaranteed CALLERROR per frame against a client
                        # that doesn't implement the property, or -- worse --
                        # a timed-out read per frame against one that simply
                        # never answers. A CALLERROR is a definitive "not
                        # implemented", so give up at once; anything else
                        # might be transient, so allow a few tries first.
                        extents_failures += 1
                        if isinstance(v, wamp.CallError) or extents_failures >= _MAX_EXTENTS_FAILURES:
                            extents_unsupported = True
                            log.info(
                                "client does not report model.extents (%r); "
                                "translation speed falls back to unit scale", v,
                            )
                        elif not have_extents:
                            log.debug("model.extents unavailable: %r", v)
                    extents_at = now_m

            # view.affine is never cached -- it also moves under the user's
            # own mouse drag. In an orthographic view our own zoom writes
            # change view.extents every frame too, so that one can't be
            # cached either; issue both at once so an orthographic view
            # costs one round trip of latency per frame, not two.
            reads = {"camera": controller.read_matrix4(navlib.PROP_VIEW_AFFINE)}
            if not cached_perspective:
                reads["view_extents"] = controller.read_box(navlib.PROP_VIEW_EXTENTS)
            got = await _gather(reads, _FRAME_READ_TIMEOUT)

            cam = got["camera"]
            if isinstance(cam, BaseException):
                # A slow frame is not a dead client: dropping navigation for
                # good on one late reply would be far more disruptive than
                # skipping a frame. Give up only once it is clearly not
                # coming back.
                camera_failures += 1
                if camera_failures >= _MAX_CAMERA_FAILURES:
                    log.warning("cannot read view.affine; navigation stopping: %s", cam)
                    return
                log.debug("view.affine read failed (%d/%d): %s",
                          camera_failures, _MAX_CAMERA_FAILURES, cam)
                continue
            camera_failures = 0

            pivot = cached_pivot
            if pivot is None:
                pivot = navlib.box_center(extents) if have_extents else (0.0, 0.0, 0.0)

            scene = nav.Scene(
                camera=cam,
                pivot=pivot,
                model_extents=extents,
                model_diagonal=navlib.box_diagonal(extents) if have_extents else 0.0,
                perspective=cached_perspective,
                rotatable=cached_rotatable,
            )
            ve = got.get("view_extents")
            if ve is not None and not isinstance(ve, BaseException):
                scene.view_extents = ve

            res = cfg.step(m, dt, scene, shaped=shaped)
            if not res.moved:
                await stop_moving()
                continue

            # Fire-and-forget from here: awaiting each of these in sequence
            # (as this used to) is five extra WAMP round trips on every
            # single frame, and that latency -- not device polling, not the
            # matrix math -- is what actually limited smoothness. See
            # bridge.Controller's cast_* docstring for why skipping the ack
            # doesn't risk a stale read on the next frame.
            try:
                if not moving:
                    moving = True
                    await controller.cast_motion(True)

                frame += 1
                await controller.cast_transaction(frame)

                if res.extents_changed and res.extents is not None:
                    await controller.cast_box(navlib.PROP_VIEW_EXTENTS, res.extents)

                await controller.cast_matrix4(navlib.PROP_VIEW_AFFINE, res.camera)
                await controller.cast_transaction(0)
            except Exception as e:  # noqa: BLE001
                log.warning("cannot write to client; navigation stopping: %s", e)
                return

    except asyncio.CancelledError:
        pass
    finally:
        await stop_moving()


async def _wait_for_tick(controller: Controller, interval: float, moving: bool):
    """Wait for the next thing that should drive a frame: the client's own
    animation clock if it's supplying one, our own ticker otherwise.
    Returns (got_client_frame, client_time_ms).

    Deliberately not a two-task race (a sleep() and a queue-get(), cancel
    whichever loses) run fresh on every single iteration: that pattern was
    creating and tearing down two asyncio tasks ~60 times a second even at
    rest, which is scheduling overhead this doesn't need. When the client
    drives frames, waiting on its queue with a timeout is the same
    information in one task instead of two, and a timeout here just means
    it stopped supplying frames (a backgrounded tab, say) -- fall back to
    our own clock rather than block forever.

    How long to wait on the client's queue depends on whether anything is
    moving, and that is the whole difference between a responsive start of
    gesture and a sluggish one. While moving, a generous `interval * 4`
    absorbs client jitter without our ticker cutting in and double-stepping.
    While stopped there are no frames to miss -- a client only animates when
    something is animating -- so waiting that long just delays noticing that
    the user has touched the puck. Together with the extra `interval` sleep
    this used to add after a timed-out wait, that came to five ticks (~83ms
    at 60Hz) of dead time at the start of every gesture, which is exactly
    where it is most visible.
    """
    if controller.client_drives_frames:
        t = await controller.next_frame_time(timeout=interval * (4 if moving else 1))
        return (True, t) if t is not None else (False, None)
    await asyncio.sleep(interval)
    return False, None


async def _gather(reads: dict, timeout: float) -> dict:
    """Await a batch of independent property reads concurrently, returning
    {name: value_or_exception}.

    Concurrency is safe here: every read gets its own WAMP call id and the
    page resolves them independently, and these are all reads, so the
    ordering guarantee the fire-and-forget writes rely on (see
    wamp.Session.cast_client) is not involved.

    Each read carries its own deadline rather than the batch sharing one.
    A batch is only ever as fast as its slowest member, so a single
    property the page accepts but never answers would otherwise hold up
    every other read in the batch for wamp's full 5s call timeout -- with
    the reads batched, that stalls navigation outright instead of merely
    making one cached value stale. One deadline per read also means a
    straggler doesn't discard the answers that did arrive.
    """
    if not reads:
        return {}
    names = list(reads)
    # Turn each read into a Task up front. Handing the raw coroutines to
    # wait_for instead leaves them unawaited if this whole batch is
    # cancelled before its wrappers get to run -- a real possibility, since
    # the drive loop is cancelled at every disconnect -- which Python
    # reports as "coroutine ... was never awaited". As tasks they are owned
    # by the loop from the start, and the finally below makes sure none
    # outlive the batch.
    tasks = {n: asyncio.ensure_future(reads[n]) for n in names}
    try:
        values = await asyncio.gather(
            *(asyncio.wait_for(asyncio.shield(tasks[n]), timeout=timeout) for n in names),
            return_exceptions=True,
        )
        return dict(zip(names, values))
    finally:
        for t in tasks.values():
            if not t.done():
                t.cancel()


async def _handle_buttons(device: SpacenavClient, controller: Controller, cfg: nav.Config, buttons: dict[int, str]) -> None:
    for ev in device.poll_events():
        if isinstance(ev, Button):
            await _apply_button(controller, cfg, buttons.get(ev.id), ev)


async def _apply_button(controller: Controller, cfg: nav.Config, action: Optional[str], b: Button) -> None:
    if action == "fit":
        if b.pressed:
            await _do_fit(controller)
    elif action == "menu":
        prop = navlib.PROP_EVENTS_KEYPRESS if b.pressed else navlib.PROP_EVENTS_KEYRELEASE
        try:
            await controller.update(prop, navlib.V3DK_MENU)
        except wamp.CallError:
            log.debug("client does not handle V3DK keys prop=%s", prop)
    elif action == "dominant-axis":
        if b.pressed:
            cfg.dominant_axis = not cfg.dominant_axis
            log.info("dominant-axis filtering on=%s", cfg.dominant_axis)
    elif action == "rotation-lock":
        if b.pressed:
            cfg.enable_rotation = not cfg.enable_rotation
            log.info("rotation enabled=%s", cfg.enable_rotation)
    elif action in (None, "none"):
        if action is None and b.pressed:
            log.info("unmapped button id=%s (map it with --buttons, e.g. --buttons 0=fit,1=menu)", b.id)


async def _do_fit(controller: Controller) -> None:
    """Frame the model, the way a driver's Fit command does."""
    got = await _gather({
        "camera": controller.read_matrix4(navlib.PROP_VIEW_AFFINE),
        "box": controller.read_box(navlib.PROP_MODEL_EXTENTS),
        "perspective": controller.read_bool(navlib.PROP_VIEW_PERSPECTIVE),
        "fov": controller.read_float(navlib.PROP_VIEW_FOV),
    }, _FIT_READ_TIMEOUT)
    cam, box = got["camera"], got["box"]
    if isinstance(cam, BaseException):
        log.warning("fit: cannot read view.affine: %s", cam)
        return
    if isinstance(box, BaseException):
        log.info("fit: the client does not report model.extents; nothing to frame")
        return

    scene = nav.Scene(camera=cam, model_extents=box, perspective=True)
    if not isinstance(got["perspective"], BaseException):
        scene.perspective = got["perspective"]
    if not scene.perspective:
        # Depends on the answer above, so it can't join the batch.
        try:
            scene.view_extents = await controller.read_box(navlib.PROP_VIEW_EXTENTS)
        except wamp.CallError:
            pass

    fov = math.radians(45)
    if not isinstance(got["fov"], BaseException) and got["fov"] > 0:
        fov = got["fov"]

    res = nav.fit(scene, fov)
    if not res.moved:
        return

    log.info("fit modelCentre=%s perspective=%s", navlib.box_center(box), scene.perspective)
    if res.extents_changed and res.extents is not None:
        try:
            await controller.write_box(navlib.PROP_VIEW_EXTENTS, res.extents)
        except wamp.CallError:
            pass
    try:
        await controller.write_matrix4(navlib.PROP_VIEW_AFFINE, res.camera)
    except wamp.CallError:
        pass
