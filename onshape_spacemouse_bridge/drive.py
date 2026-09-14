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
            await asyncio.wait_for(controller.set_motion(False), timeout=1.0)
        except (asyncio.TimeoutError, wamp.CallError):
            pass

    try:
        while True:
            dt, got_client_frame, client_t = await _wait_for_tick(controller, interval)
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
            if cfg.shape_is_zero(m):
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
            if not have_extents or (not moving and (now_m - extents_at) > _EXTENTS_TTL):
                try:
                    extents = await controller.read_box(navlib.PROP_MODEL_EXTENTS)
                    have_extents = True
                except wamp.CallError:
                    pass
                except Exception as e:  # noqa: BLE001
                    if not have_extents:
                        log.debug("model.extents unavailable: %s", e)
                extents_at = now_m

            try:
                cam = await controller.read_matrix4(navlib.PROP_VIEW_AFFINE)
            except Exception as e:  # noqa: BLE001
                log.warning("cannot read view.affine; navigation stopping: %s", e)
                return

            if not have_scene_props or (not moving and (now_m - scene_props_at) > _SCENE_PROPS_TTL):
                try:
                    cached_pivot = await controller.read_vec3(navlib.PROP_PIVOT_POSITION)
                except wamp.CallError:
                    pass
                try:
                    cached_rotatable = await controller.read_bool(navlib.PROP_VIEW_ROTATABLE)
                except wamp.CallError:
                    pass
                try:
                    cached_perspective = await controller.read_bool(navlib.PROP_VIEW_PERSPECTIVE)
                except wamp.CallError:
                    pass
                have_scene_props = True
                scene_props_at = now_m

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
            if not cached_perspective:
                # Changes under our own zoom writes every frame, unlike the
                # other scene properties above, so this one can't be cached.
                try:
                    scene.view_extents = await controller.read_box(navlib.PROP_VIEW_EXTENTS)
                except wamp.CallError:
                    pass

            res = cfg.step(m, dt, scene)
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


async def _wait_for_tick(controller: Controller, interval: float):
    """Wait for the next thing that should drive a frame: the client's own
    animation clock if it's supplying one, our own ticker otherwise.

    Deliberately not a two-task race (a sleep() and a queue-get(), cancel
    whichever loses) run fresh on every single iteration: that pattern was
    creating and tearing down two asyncio tasks ~60 times a second even at
    rest, which is scheduling overhead this doesn't need. When the client
    drives frames, waiting on its queue with a timeout is the same
    information in one task instead of two, and a timeout here just means
    it stopped supplying frames (a backgrounded tab, say) -- fall back to
    our own clock rather than block forever.
    """
    if controller.client_drives_frames:
        t = await controller.next_frame_time(timeout=interval * 4)
        if t is not None:
            return None, True, t
    await asyncio.sleep(interval)
    return None, False, None


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
    try:
        cam = await controller.read_matrix4(navlib.PROP_VIEW_AFFINE)
        box = await controller.read_box(navlib.PROP_MODEL_EXTENTS)
    except wamp.CallError:
        log.info("fit: the client does not report model.extents; nothing to frame")
        return

    scene = nav.Scene(camera=cam, model_extents=box, perspective=True)
    try:
        scene.perspective = await controller.read_bool(navlib.PROP_VIEW_PERSPECTIVE)
    except wamp.CallError:
        pass
    if not scene.perspective:
        try:
            scene.view_extents = await controller.read_box(navlib.PROP_VIEW_EXTENTS)
        except wamp.CallError:
            pass

    fov = math.radians(45)
    try:
        v = await controller.read_float(navlib.PROP_VIEW_FOV)
        if v > 0:
            fov = v
    except wamp.CallError:
        pass

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
