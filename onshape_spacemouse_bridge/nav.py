"""Turns SpaceMouse deflection into camera motion.

This is the part 3Dconnexion ships as a closed binary: the driver reads the
application's camera and scene, computes a new pose, and writes it back (see
navlib.py's module docstring). Everything here is a pure function of input
plus scene, so it can be reasoned about without a device or a browser -- but
the constants are feel, not physics.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from . import navlib
from .spacenav import Motion

MODE_OBJECT = "object"  # push right -> the MODEL goes right (camera moves left). CAD default.
MODE_CAMERA = "camera"  # push right -> the CAMERA goes right.

FIT_PADDING = 1.15


@dataclass
class Config:
    """Tunes the response. Speeds are relative to the model diagonal so a
    large assembly and a small part both feel the same.
    """

    mode: str = MODE_OBJECT

    # Device magnitude at full deflection. 350 is the HID logical maximum a
    # 3Dconnexion device declares for all six axes, and spacenavd multiplies
    # by `sensitivity` (default 1.0) with no clamp -- so a machine with no
    # /etc/spnavrc saturates at exactly 350. This is the stock case, not a
    # guess; only a tuned spnavrc needs a different number here.
    full_scale: float = 350.0
    # Rotation does not share a range with translation on real hardware --
    # measuring "push in any direction" gave different peaks depending on
    # whether the cap was slid or tipped. Zero falls back to full_scale.
    rotation_full_scale: float = 350.0

    # Fraction of full scale below which input is ignored. Must exceed the
    # device's resting noise floor (a SpaceMouse Compact idles around 4% of
    # full scale) or the view drifts while nobody is touching it.
    deadzone: float = 0.06
    # Response curve: 1 is linear, higher gives finer control near centre
    # while keeping full speed at the extremes.
    exponent: float = 1.6

    translation_speed: float = 0.9  # model diagonals / second at full deflection
    rotation_speed: float = 1.6  # radians / second at full deflection
    # Orthographic zoom: e-foldings/second at full deflection. Exponential so
    # zooming in and back out returns exactly where it started, independent
    # of frame rate.
    zoom_speed: float = 1.2

    # Suppresses every axis but the strongest. Off by default: cross-talk
    # turned out to depend on how deliberately the cap is moved, not to be
    # inherent to the device.
    dominant_axis: bool = False

    enable_translation: bool = True
    enable_rotation: bool = True

    def _shape(self, v: float, full_scale: float) -> float:
        """Clamp, deadzone with rescaling so there is no jump at the edge,
        then the response curve.
        """
        if full_scale <= 0:
            return 0.0
        n = v / full_scale
        n = max(-1.0, min(1.0, n))
        a = abs(n)
        if a <= self.deadzone:
            return 0.0
        if self.deadzone < 1:
            a = (a - self.deadzone) / (1 - self.deadzone)
        if self.exponent > 0 and self.exponent != 1:
            a = a ** self.exponent
        return math.copysign(a, n)

    def _shape_all(self, m: Motion) -> dict:
        rot_scale = self.rotation_full_scale if self.rotation_full_scale > 0 else self.full_scale
        a = {
            "x": self._shape(m.x, self.full_scale),
            "y": self._shape(m.y, self.full_scale),
            "z": self._shape(m.z, self.full_scale),
            "rx": self._shape(m.rx, rot_scale),
            "ry": self._shape(m.ry, rot_scale),
            "rz": self._shape(m.rz, rot_scale),
        }
        if self.dominant_axis:
            a = _keep_largest(a)
        return a

    def shape_is_zero(self, m: Motion) -> bool:
        """Whether the input falls entirely inside the deadzone. Call this
        before reading anything from the client: when the user isn't
        touching the device there's no reason to spend a round trip.
        """
        return all(v == 0 for v in self._shape_all(m).values())

    def step(self, m: Motion, dt_seconds: float, scene: "Scene") -> "Result":
        """Compute the next camera pose. `result.moved` is False when the
        input was inside the deadzone and the camera should be left alone.

        Device axes are Z-up right-handed (X right, Y away, Z up); the
        camera frame is OpenGL's (X right, Y up, -Z forward), which is what
        view.affine uses. The (x, z, -y) swap below is the single place that
        mapping is encoded.
        """
        out = Result(camera=list(scene.camera))
        a = self._shape_all(m)
        if all(v == 0 for v in a.values()):
            return out
        if dt_seconds <= 0:
            return out

        diagonal = scene.model_diagonal if scene.model_diagonal > 0 else 1.0
        # In object mode the model follows the cap, so the camera moves opposite.
        sign = -1.0 if self.mode == MODE_OBJECT else 1.0

        if self.enable_rotation and scene.rotatable and (a["rx"] or a["ry"] or a["rz"]):
            local = (a["rx"], a["rz"], -a["ry"])
            scale = sign * self.rotation_speed * dt_seconds
            local = (local[0] * scale, local[1] * scale, local[2] * scale)
            angle = math.sqrt(sum(c * c for c in local))
            if angle > 0:
                # Direction is computed from the scene's camera as handed to
                # us this frame, not from `out.camera`; the two matrices are
                # composed in sequence below regardless.
                axis_world = navlib.mat_mul_vec(scene.camera, local)
                r = navlib.rotate_axis(axis_world, angle)
                out.camera = navlib.mat_mul(navlib.orbit_about(scene.pivot, r), out.camera)
                out.moved = True

        if self.enable_translation and (a["x"] or a["y"] or a["z"]):
            local = [a["x"], a["z"], -a["y"]]
            scale = sign * self.translation_speed * diagonal * dt_seconds
            local = [local[0] * scale, local[1] * scale, local[2] * scale]

            ortho = (not scene.perspective) and not navlib.box_empty(scene.view_extents)
            if ortho:
                # Dollying an orthographic camera is a no-op on screen; the
                # equivalent is scaling the view box. Positive camera-local Z
                # is backwards, away from the subject, so it zooms out.
                zoom_in = local[2]
                local[2] = 0
                if zoom_in != 0:
                    f = math.exp(self.zoom_speed * sign * -zoom_in / (self.translation_speed * diagonal))
                    out.extents = navlib.box_scaled(scene.view_extents, f)
                    out.extents_changed = True
                    out.moved = True

            if local[0] or local[1] or local[2]:
                world = navlib.mat_mul_vec(scene.camera, local)
                out.camera = navlib.mat_mul(navlib.translate(world), out.camera)
                out.moved = True

        return out


def _keep_largest(a: dict) -> dict:
    win = max(a, key=lambda k: abs(a[k]))
    return {k: (v if k == win else 0.0) for k, v in a.items()}


@dataclass
class Scene:
    """What the client told us about its view and model."""

    camera: navlib.Mat4  # navlib.PROP_VIEW_AFFINE, in canonical form
    pivot: navlib.Vec3 = (0.0, 0.0, 0.0)  # world-space point rotation happens about
    model_diagonal: float = 0.0  # falls back to 1 (unit/sec) rather than nothing
    model_extents: Optional[navlib.Box] = None  # needed for Fit; optional for Step

    perspective: bool = True
    view_extents: Optional[navlib.Box] = None  # ignored unless orthographic
    rotatable: bool = True


@dataclass
class Result:
    camera: navlib.Mat4
    extents: Optional[navlib.Box] = None
    moved: bool = False
    extents_changed: bool = False


def fit(scene: Scene, fov: float) -> Result:
    """Frame the model in the view, keeping the camera's current orientation
    -- what a Fit button does. Computed here rather than delegated to the
    application, so it works with any client.

    fov is the vertical field of view in radians; ignored for orthographic
    views, where the view box is resized instead of moving the camera.
    """
    out = Result(camera=list(scene.camera))
    if navlib.box_empty(scene.model_extents):
        return out

    centre = navlib.box_center(scene.model_extents)
    radius = navlib.box_diagonal(scene.model_extents) / 2
    if radius <= 0:
        return out

    # The camera's own +Z points backwards, away from what it is looking at.
    back = navlib.mat_mul_vec(scene.camera, (0.0, 0.0, 1.0))
    n = math.sqrt(sum(c * c for c in back))
    back = tuple(c / n for c in back) if n > 0 else (0.0, 0.0, 1.0)

    if scene.perspective:
        half = fov / 2
        if half <= 0 or half >= math.pi / 2:
            half = math.radians(22.5)  # a sane 45-degree default
        distance = radius / math.sin(half) * FIT_PADDING
    else:
        distance = radius * 4
        if not navlib.box_empty(scene.view_extents):
            ve = scene.view_extents
            half_w = (ve[3] - ve[0]) / 2
            half_h = (ve[4] - ve[1]) / 2
            aspect = half_w / half_h if half_h > 0 else 1.0
            h = radius * FIT_PADDING
            w = h * aspect
            out.extents = (-w, -h, ve[2], w, h, ve[5])
            out.extents_changed = True

    pos = (centre[0] + back[0] * distance, centre[1] + back[1] * distance, centre[2] + back[2] * distance)
    cam = list(scene.camera)
    cam[12], cam[13], cam[14] = pos
    out.camera = cam
    out.moved = True
    return out
