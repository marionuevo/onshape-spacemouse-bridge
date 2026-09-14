"""Property model and matrix math for the 3Dconnexion Navigation Library
interface as exposed to web clients by 3DconnexionJS.

The key inversion to keep in mind: the *page* serves these properties and
this process consumes them. We read the camera and scene, compute a new
camera pose, and write it back. The page is a property server; this program
is the property client -- the opposite of what "driver" suggests.

Property names match navlib.h / 3dconnexion.js's clientFnRead/clientFnUpdate
maps. Unknown or unimplemented properties are answered with a CALLERROR by
the page and that is normal -- see wamp.is_unsupported.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

# --- Properties ------------------------------------------------------------

PROP_VIEW_AFFINE = "view.affine"
PROP_VIEW_EXTENTS = "view.extents"
PROP_VIEW_FOV = "view.fov"
PROP_VIEW_PERSPECTIVE = "view.perspective"
PROP_VIEW_TARGET = "view.target"
PROP_VIEW_ROTATABLE = "view.rotatable"

PROP_MODEL_EXTENTS = "model.extents"

PROP_PIVOT_POSITION = "pivot.position"
PROP_PIVOT_VISIBLE = "pivot.visible"

PROP_MOTION = "motion"
PROP_TRANSACTION = "transaction"

PROP_EVENTS_KEYPRESS = "events.keyPress"
PROP_EVENTS_KEYRELEASE = "events.keyRelease"
PROP_VIEWS_FRONT = "views.front"

PROC_READ = "self:read"
PROC_UPDATE = "self:update"

# V3DK button codes, from 3dconnexion.js.
V3DK_MENU = 0x1E
V3DK_FIT = 0x1F

LAYOUT_COLUMN_MAJOR = "column-major"
LAYOUT_ROW_MAJOR = "row-major"


@dataclass
class ClientInfo:
    """What the page sent when creating a 3dcontroller -- the only reliable
    signal for the client's capabilities and matrix layout.
    """

    name: str = ""
    version: float = 0.0
    row_major_order: Optional[bool] = None


@dataclass
class Quirks:
    """Per-client behavioural differences. There was a breaking change at
    3DconnexionJS v0.5: column-major (translation at flat indices 12-14)
    became the default, with row-major requested via rowMajorOrder. Onshape
    (measured 0.6.0) is column-major with no rowMajorOrder field -- which is
    why version alone must decide when that field is absent.
    """

    layout: str = LAYOUT_COLUMN_MAJOR
    frame_timing: bool = False


def quirks_for(info: ClientInfo) -> Quirks:
    q = Quirks(layout=LAYOUT_COLUMN_MAJOR, frame_timing=info.version >= 0.6)
    if info.row_major_order is not None:
        q.layout = LAYOUT_ROW_MAJOR if info.row_major_order else LAYOUT_COLUMN_MAJOR
    elif info.version < 0.5:
        q.layout = LAYOUT_ROW_MAJOR
    return q


# --- Mat4: flat 16-element list, canonical internal form -------------------
#
# Column-major storage: element (row, col) lives at m[col*4 + row], and the
# translation column occupies indices 12, 13, 14. Wire data arrives in
# whichever layout the client uses (see Quirks); convert with `canonical`
# and `from_canonical` rather than doing arithmetic on wire values directly.

Mat4 = list  # 16 floats
Vec3 = tuple  # 3 floats
Box = tuple  # 6 floats: minx, miny, minz, maxx, maxy, maxz


def identity4() -> Mat4:
    return [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]


def _at(m: Mat4, row: int, col: int) -> float:
    return m[col * 4 + row]


def mat_mul(a: Mat4, b: Mat4) -> Mat4:
    """a * b, applied to column vectors right to left."""
    out = [0.0] * 16
    for col in range(4):
        for row in range(4):
            s = 0.0
            for k in range(4):
                s += _at(a, row, k) * _at(b, k, col)
            out[col * 4 + row] = s
    return out


def mat_transpose(m: Mat4) -> Mat4:
    out = [0.0] * 16
    for col in range(4):
        for row in range(4):
            out[col * 4 + row] = _at(m, col, row)
    return out


def mat_mul_vec(m: Mat4, v: Sequence[float]) -> Vec3:
    """Transform a direction (w=0): rotation and scale apply, translation
    does not. Use for axes and offsets expressed in a local frame.
    """
    return tuple(
        _at(m, row, 0) * v[0] + _at(m, row, 1) * v[1] + _at(m, row, 2) * v[2]
        for row in range(3)
    )


def translation_of(m: Mat4) -> Vec3:
    return (m[12], m[13], m[14])


def translate(t: Sequence[float]) -> Mat4:
    m = identity4()
    m[12], m[13], m[14] = t[0], t[1], t[2]
    return m


def rotate_axis(axis: Sequence[float], angle: float) -> Mat4:
    """A rotation of `angle` radians about `axis` (need not be normalised).
    A zero-length axis yields the identity.
    """
    length = math.sqrt(sum(a * a for a in axis))
    if length == 0:
        return identity4()
    x, y, z = axis[0] / length, axis[1] / length, axis[2] / length
    c, s = math.cos(angle), math.sin(angle)
    t = 1 - c

    m = identity4()

    def set_(row, col, v):
        m[col * 4 + row] = v

    set_(0, 0, t * x * x + c)
    set_(0, 1, t * x * y - s * z)
    set_(0, 2, t * x * z + s * y)
    set_(1, 0, t * x * y + s * z)
    set_(1, 1, t * y * y + c)
    set_(1, 2, t * y * z - s * x)
    set_(2, 0, t * x * z - s * y)
    set_(2, 1, t * y * z + s * x)
    set_(2, 2, t * z * z + c)
    return m


def orbit_about(pivot: Sequence[float], r: Mat4) -> Mat4:
    """The transform that rotates about a world-space pivot: T(pivot) * R * T(-pivot)."""
    neg = (-pivot[0], -pivot[1], -pivot[2])
    return mat_mul(mat_mul(translate(pivot), r), translate(neg))


def canonical(wire_values: Sequence[float], layout: str) -> Mat4:
    """Wire data (flat 16 floats, in the client's layout) -> internal
    column-major form. Converting between the two wire layouts is exactly a
    transpose.
    """
    m = list(wire_values)
    if layout == LAYOUT_ROW_MAJOR:
        return mat_transpose(m)
    return m


def from_canonical(m: Mat4, layout: str) -> list:
    if layout == LAYOUT_ROW_MAJOR:
        return mat_transpose(m)
    return list(m)


# --- Box: [minx, miny, minz, maxx, maxy, maxz] ------------------------------


def box_center(b: Optional[Sequence[float]]) -> Vec3:
    if b is None:
        return (0.0, 0.0, 0.0)
    return ((b[0] + b[3]) / 2, (b[1] + b[4]) / 2, (b[2] + b[5]) / 2)


def box_diagonal(b: Optional[Sequence[float]]) -> float:
    if b is None:
        return 0.0
    return math.sqrt((b[3] - b[0]) ** 2 + (b[4] - b[1]) ** 2 + (b[5] - b[2]) ** 2)


def box_empty(b: Optional[Sequence[float]]) -> bool:
    return b is None or (b[0] == b[3] and b[1] == b[4] and b[2] == b[5])


def box_scaled(b: Sequence[float], f: float) -> Box:
    """Scale a box about its own center by factor f."""
    cx, cy, cz = box_center(b)
    return (
        cx + (b[0] - cx) * f, cy + (b[1] - cy) * f, cz + (b[2] - cz) * f,
        cx + (b[3] - cx) * f, cy + (b[4] - cy) * f, cz + (b[5] - cz) * f,
    )
