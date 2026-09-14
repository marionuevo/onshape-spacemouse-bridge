# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Onshape's 3Dconnexion (SpaceMouse) integration expects the vendor's
Windows/macOS driver to be running locally, serving a fixed protocol at
`wss://127.51.68.120:8181`. That driver has no Linux build. This program
*is* that missing driver for Linux: it reads 6-DoF input from `spacenavd`
(the daemon Blender/FreeCAD already use) and speaks the same protocol back
to Onshape's page, so a SpaceMouse drives the camera the same way it does
on Windows/macOS.

Read `README.md` first — it has the full "why", the setup/troubleshooting
steps, and a "Performance and smoothness" section explaining the real bugs
that were fixed (a stale-timestamp bug causing camera jumps, a
stuck-rotation bug from `spacenavd` going quiet on release, and a later
round covering certificate renewal, client-version parsing, `delete`
handling, and shutdown). Don't re-introduce any of them — the fixes are
explained there and in the relevant docstrings (`drive.py`, `spacenav.py`,
`certs.py`, `navlib.parse_version`).

## Commands

No dependency manifest (no `requirements.txt`/`pyproject.toml`) — the three
dependencies (`python-aiohttp`, `python-cryptography`, `nss`) are installed
as system (pacman) packages, not a venv. Run everything with the system
`python3`.

```sh
# Syntax-check everything (there is no test suite — see "Testing" below)
python3 -m py_compile main.py onshape_spacemouse_bridge/*.py

# Run the bridge in the foreground, verbose
python3 main.py serve -v

# One-time cert generation + Brave/Chrome trust store injection
python3 main.py gen-certs
python3 main.py trust

# Optionally restrict which sites may use the bridge (default: any, as the
# real driver is). Refuses others with 403 at discovery and the WS upgrade.
python3 main.py serve --allowed-origins https://cad.onshape.com

# Confirm spacenavd is producing events, independent of the browser/WAMP side
python3 main.py read-mouse

# It also runs as a systemd --user service (see README's "Running it at
# login" section for the enable/disable/logs commands):
systemctl --user status|restart|disable onshape-spacemouse-bridge
journalctl --user -u onshape-spacemouse-bridge -f
```

Only one instance can hold port 8181 (`127.51.68.120:8181`, hardcoded by
Onshape's client library, not configurable) — stop a manually-run `serve`
before starting/restarting the systemd service, or vice versa.

## Testing

There is no automated test suite in this repo. Verification during
development was done with throwaway scratch scripts (in-process fakes for
the WebSocket/page side, driving the real `wamp`/`bridge`/`nav`/`drive`
modules directly with no real network or TLS involved) — none were
committed. If you add tests, that in-process-fake-page approach is the
right shape: real handshake and RPC dispatch, but a scripted "page" replying
to `self:read`/`self:update` instead of a browser. `nav.py`'s functions
(`Config.step`, `fit`) are pure and the easiest entry point for unit tests.

## Architecture

Layered from the device up to the browser; each module is one layer, and
the flow of a navigation frame passes through all of them in order:

```
spacenavd (system daemon)
  -> spacenav.py   Client: reads /var/run/spnav.sock, latches Motion/Button
  -> nav.py        Config.step(): device deflection + current camera -> new camera
  -> navlib.py     Mat4 math (column-major canonical form) + property constants
  -> bridge.py     Controller: read/write navlib properties on the page;
                    Bridge: the create/update/delete + subscribe handshake
  -> wamp.py       WAMP v1 framing, the reverse-RPC-via-EVENT trick, Session
  -> drive.py       the ~60Hz per-connection loop tying the above together
  -> server.py      aiohttp: HTTPS discovery endpoint + the WAMP WebSocket
  -> certs.py       local CA/leaf generation + NSS (Brave/Chrome) trust injection
```

Things that aren't obvious from any single file:

- **The page is the property *server*; this program is the property
  *client*.** Backwards from what "driver" suggests: we don't push raw
  device deltas to Onshape, we read Onshape's camera/scene state
  (`view.affine`, `model.extents`, ...), compute a new camera pose in
  `nav.py`, and write it back. See `navlib.py`'s module docstring.
- **WAMP v1 has no server-initiated CALL.** The driver (us) has to *ask*
  the page to read/write properties, but the client never calls us first.
  The workaround, straight from the real 3Dconnexion client library: publish
  an `EVENT` to the page's subscribed topic whose payload is itself a `CALL`
  message; the page unwraps it and replies with a bare `CALLRESULT`. See
  `wamp.py`'s module docstring and `Session.call_client`/`cast_client`.
- **Matrix layout is per-client, not fixed.** Pre-0.5 3DconnexionJS clients
  are row-major; 0.5+ is column-major unless `rowMajorOrder` says otherwise.
  `navlib.quirks_for()` derives it from the `ClientInfo` the page sends at
  handshake time. Onshape (0.6.0, confirmed on the wire) is column-major
  with no `rowMajorOrder` field — get the version-based fallback in
  `quirks_for` wrong and every write silently transposes the camera.
  `navlib.canonical`/`from_canonical` are the only places layout conversion
  should happen. A version that can't be read is `navlib.UNKNOWN_VERSION`,
  deliberately distinct from `0.0`: unknown must route to the modern
  column-major default, never to the pre-0.5 row-major fallback.
- **`drive.py`'s hot path is fire-and-forget by design.** `Controller.cast_*`
  sends a WAMP write without awaiting the page's acknowledgement — awaiting
  every property read/write in sequence (the naive port from the reference
  Go implementation) was the actual cause of visible stutter, not device
  polling or the matrix math. This is safe only because WebSocket preserves
  message order and the page handles messages one at a time; see
  `wamp.Session.cast_client`'s docstring before changing this.
- **Scene-property caching (`model.extents`, `pivot.position`,
  `view.rotatable`, `view.perspective`) only refreshes between motion
  bursts, gated on `not moving` in `drive.py`.** Refreshing mid-gesture was
  an actual bug (a cache change mid-rotation reads as the camera jumping).
  `view.affine` itself is never cached — it also changes from the user's
  own mouse drag.
- **Reads that are due together go through `drive._gather`, not sequential
  awaits.** They're independent properties with their own call ids, so
  awaiting them in series multiplies latency for nothing. Two rules hold it
  together: each read gets its *own* deadline (a batch is only as fast as
  its slowest member, and one property the page never answers would
  otherwise stall navigation outright), and each is wrapped in a Task up
  front (raw coroutines handed to `wait_for` are left unawaited if the
  batch is cancelled first — which happens at every disconnect).
- **Live WebSockets must be closed on shutdown (`server._close_websockets`,
  wired to `app.on_shutdown`).** aiohttp waits for handlers to return and
  `async for msg in ws` never does, so with a browser connected the process
  ignored SIGTERM until systemd's 90s timeout and SIGKILL. That makes
  `systemctl --user restart` unusable, so don't drop the tracking set.
- **`127.51.68.120:8181` cannot be changed** — it's hardcoded in Onshape's
  bundled `3dconnexion.js`. This is also why a locally-generated CA (not a
  public one — CA/Browser Forum rules forbid issuing for loopback IPs) is
  unavoidable; see `certs.py`'s module docstring for the CA+leaf design and
  why the CA private key must never be shipped/synced anywhere.
- **Only Chromium/Brave NSS trust injection is implemented**
  (`certs.trust_chromium`, targeting the shared `~/.pki/nssdb`). Firefox
  uses a different per-profile mechanism (`cert_override.txt`) and isn't
  wired up.
