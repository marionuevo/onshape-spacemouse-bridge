# onshape-spacemouse-bridge

Makes a 3Dconnexion SpaceMouse / SpaceNavigator drive the camera in Onshape,
running in Brave (or any Chromium browser) on Linux.

## Why this exists

Onshape's SpaceMouse support is not WebHID and it is not a Brave-specific
block. `3dconnexion.js`, the client library Onshape loads, talks to a fixed
local address, `wss://127.51.68.120:8181`, that's normally served by
3Dconnexion's own Windows or macOS driver. **3Dconnexion has no Linux
driver.** That's why it works on your Mac and not here — Chrome, Firefox and
Brave on Linux would all fail identically, because nothing is listening on
that address.

This program is that missing driver: it reads 6-DoF input from `spacenavd`
(the same daemon Blender and FreeCAD already use) and speaks the same
protocol back to the page that the real driver would.

Full protocol notes — the WAMP v1 handshake, the property model, the
navigation math, the certificate design — are inline as docstrings in
`onshape_spacemouse_bridge/*.py`; start with `wamp.py` and `navlib.py` if you
want the wire-level detail.

## How it moves the camera

Push the cap right and the **model** goes right (the camera moves left to
compensate) — that's `nav.Config`'s default `mode="object"`, matching
3Dconnexion's own CAD default and what you're used to in Onshape. Pass
`--mode camera` to invert that.

## Performance and smoothness

The obvious way to implement `drive.py`'s per-frame loop — read
`view.affine`, `pivot.position`, `view.rotatable`, `view.perspective`, then
write `motion`/`transaction`/`view.affine`/`transaction` again, awaiting each
WAMP round trip before sending the next — works, but noticeably less
smoothly than the real driver on macOS. None of that is network latency
(everything is loopback TLS on the same machine); it's the accumulated cost
of doing 8-9 *sequential, blocking* round trips every single frame, each
paying its own JSON-encode + WebSocket-frame + asyncio-scheduling overhead.
At a 16.7ms budget for 60fps, that adds up fast in Python in a way it
wouldn't in a compiled driver. Three changes fixed it, in order of impact:

1. **Fire-and-forget writes.** `motion`, `transaction`, `view.affine` and
   `view.extents` writes now use `Controller.cast_*` /
   `Session.cast_client`, which send the WAMP message but don't wait for the
   page's acknowledgement — cutting the awaited round trips on the hot path
   from ~8 to essentially 1 (`view.affine`, which must stay a live read;
   see below). This is safe: WebSocket delivers messages in order and the
   page's JS handles them one at a time, so a write we don't wait for is
   still guaranteed to be applied before whatever we read next arrives at
   the page — see `wamp.Session.cast_client`'s docstring.
2. **Caching properties that rarely change.** `model.extents`,
   `pivot.position`, `view.rotatable` and `view.perspective` are cached with
   a short TTL instead of re-read every frame — but **only refreshed
   between motion bursts** (`not moving`), never mid-gesture. Refreshing
   mid-burst was an early mistake: if the cached pivot or model diagonal
   changed while a rotation was actively being applied, the camera would
   visibly jump the instant the cache refreshed. `view.affine` itself is
   never cached — it also changes from the user's own mouse drag, not only
   from our writes.
3. **Following the client's own frame clock.** Onshape sets
   `frame.timingSource` and drives its own rAF loop; `drive.py` waits on
   that instead of racing a separate ticker task against it every
   iteration, which cuts needless asyncio task churn too. How long it waits
   depends on whether anything is moving: a generous four ticks while
   moving (absorbing client jitter without our own ticker cutting in and
   double-stepping), but only one while stopped, where there are no frames
   to miss and a long wait only delays noticing that the user has touched
   the puck.
4. **Issuing independent reads concurrently.** The reads that do remain are
   sent as one batch (`drive._gather`) rather than awaited one after
   another. Each carries its own WAMP call id and the page answers them
   independently, so four sequential round trips were costing four times
   the latency of one for no benefit. Each read carries its own deadline
   rather than the batch sharing one — a batch is only ever as fast as its
   slowest member, and a single property the page accepts but never answers
   would otherwise stall navigation outright instead of merely leaving one
   cached value stale.

Between them, a perspective frame costs one blocking round trip
(`view.affine`) and an orthographic one also costs one, not two. Measured
on the dead time between deflecting the puck and the first camera write:
**~83ms down to ~25ms**.

Whether Go instead of Python would help further: probably not much, for
this specific shape of workload. A compiled binary has lower per-message
overhead (no GIL, faster JSON), but that mostly matters under many
concurrent connections or high throughput — here it's one connection at
~60fps with small JSON messages over loopback, and the fixes above already
got the blocking round trips per frame down to one. The two smoothness bugs
actually hit during development (below) were both logic errors, not
anything a faster language would have sidestepped.

### Two smoothness bugs found the hard way

**Occasional huge jump.** `drive.py` computes `dt` either from Onshape's own
frame timestamps or, as a fallback, from a wall-clock `last` timestamp. An
early version only refreshed `last` inside the fallback branch; since
Onshape almost always supplies its own frame times, `last` could sit stale
for minutes. The moment the fallback path fired even once — one dropped
`frame.time` update is enough — `dt` came out as "however long it had been
since `last` last updated" instead of one tick, and the camera jumped by
however many seconds that was in a single step. Fixed by refreshing `last`
on every loop iteration regardless of which branch supplied `dt`, plus a
100ms cap on the fallback `dt` as a second line of defence.

**Kept rotating until the device was recentred by hand.** `spacenav.Client`
used to report whatever `spacenavd` last sent, forever. In practice, a fast
flick-and-release doesn't always end with spacenavd re-reporting a "back to
zero" sample — depending on the device and how abruptly it's released, the
value can go from clearly-deflected to at-rest between two polls with
nothing in between, and unlike a *held* off-centre position (which does
keep streaming), a quiet return to centre may just... stop being reported.
`Client.state()` now returns a centred `Motion` if nothing has arrived from
spacenavd in the last 150ms (well over spacenavd's ~8ms/125Hz period),
instead of trusting a value that might be stale forever.

### Bugs found in a later review

Less visible than the two above, but each a real failure mode:

- **Certificate renewal silently broke browser trust.** `ensure()` runs on
  every `serve`, and regenerated the CA *and* leaf once the leaf neared
  expiry. Since both share a subject name, a name-only trust check still
  reported success while the browser rejected every connection — roughly
  two years in, the bridge would simply have stopped working. Renewal now
  re-issues only the leaf, from the existing CA (`certs.renew_leaf`), and
  `trust` compares the installed CA's DER against the one on disk.
- **A dotted client version killed the handshake.** `float("0.6.0")` raises,
  and inside the create handler that became a CALLERROR that aborted the
  whole handshake with the device appearing dead. `navlib.parse_version`
  never raises, and an unreadable version is now distinct from `0.0` —
  falling back on "older than 0.5, so row-major" would silently transpose
  every camera write.
- **`delete` followed by a re-subscribe started a second drive loop.**
  `_handle_delete` cleared only the controller, leaving `_instance_id` set,
  so a later SUBSCRIBE built a second Controller while the first loop kept
  writing `view.affine` on its old topic, holding whatever focus it last
  saw. The handshake state is now fully reset and the running loop stopped.
- **An unsupported `model.extents` was re-read every frame.** Leaving
  `have_extents` False kept the read due regardless of its TTL.
- **The service never shut down while a browser was connected.** aiohttp
  waits for WebSocket handlers to return, and `async for msg in ws` never
  does. `systemctl --user restart` therefore sat through the full 90s stop
  timeout and ended in SIGKILL; `build_app` now closes live sockets in
  `on_shutdown` (measured: 90s + SIGKILL down to ~0.1s).
- **Orthographic zoom ignored `--mode camera`.** `sign` was applied twice
  and cancelled, so an orthographic view zoomed the opposite way from a
  perspective one for the same gesture. Only affects `--mode camera`; the
  default `object` mode was correct by coincidence.

## One-time setup

Dependencies (`python-aiohttp`, `python-cryptography`, `nss`) are already on
this machine via pacman. Elsewhere: `sudo pacman -S python-aiohttp
python-cryptography nss` (or `omarchy pkg add` on Omarchy).

```sh
cd ~/Work/onshape-spacemouse-bridge

# 1. Generate a local CA + certificate for 127.51.68.120. A public CA can
#    never issue one for a loopback address, so this is unavoidable — it's
#    exactly what 3Dconnexion's own installer does on Windows/macOS.
python3 main.py gen-certs

# 2. Install the CA into Brave/Chrome's shared trust store (~/.pki/nssdb).
#    Close Brave first -- it reads that store at startup, not on demand.
python3 main.py trust
```

The CA private key lives at
`~/.local/share/onshape-spacemouse-bridge/certs/ca-key.pem`, mode `600`,
and never leaves this machine.

## Running it

For a first try, or when actively debugging:

```sh
python3 main.py serve -v
```

Leave it running, then open a Part Studio or Assembly (a document with a 3D
viewport — not the dashboard) in Brave. `-v` logs each connection's
handshake, so you'll see `3dcontroller created client=Onshape` and then
`navigation active` if everything is wired up.

Any page your browser loads can open a connection here, exactly as it can to
the real 3Dconnexion driver — a WebSocket has no same-origin protection, and
the driver has no way to know which sites are legitimate. In practice a
connection can only drive its *own* page's camera, and only while that page
reports having focus, so the exposure is small. If you'd rather lock it down
anyway:

```sh
python3 main.py serve --allowed-origins https://cad.onshape.com
```

Anything else is then refused with a 403 at both the discovery endpoint and
the WebSocket upgrade. The default stays unrestricted, because an allowlist
would break any other 3DconnexionJS client you point at it.

### Running it at login (recommended once it's working)

A systemd user service starts it automatically every login and restarts it
if it ever crashes:

```sh
mkdir -p ~/.config/systemd/user
cp systemd/onshape-spacemouse-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now onshape-spacemouse-bridge
```

`enable --now` both starts it immediately and marks it to start at every
future login (via `default.target` — this is a plain systemd --user
service, unrelated to Hyprland/Omarchy's own `exec-once` autostart
mechanism, and works the same under any desktop). Manage it like any other
user service:

```sh
systemctl --user status onshape-spacemouse-bridge     # running? for how long?
journalctl --user -u onshape-spacemouse-bridge -f     # follow the logs
systemctl --user restart onshape-spacemouse-bridge    # e.g. after editing the code
systemctl --user disable --now onshape-spacemouse-bridge  # stop, and stop autostarting
```

Only one instance can hold port 8181 at a time — stop a manually-run `serve`
(Ctrl-C, or `pkill -f 'main.py serve'`) before starting the service, and
vice versa.

## Troubleshooting

**Blank/no reaction in Onshape, no certificate warning.** A cross-origin
request to an untrusted certificate fails silently in the browser — no
prompt, indistinguishable from Onshape simply not trying. Re-run `trust`
with Brave fully closed (`pgrep -fi brave` should print nothing), then
restart Brave.

**Verify the discovery endpoint directly:**

```sh
curl --cacert ~/.local/share/onshape-spacemouse-bridge/certs/ca.pem \
  https://127.51.68.120:8181/3dconnexion/nlproxy
# {"port": 8181, "version": "1.4.8.21486"}
```

If that fails, the bridge isn't running or isn't bound — check `serve`'s
output. If it succeeds but Onshape still does nothing, the certificate
likely isn't trusted by Brave specifically (re-run `trust`; make sure Brave
was closed) or Brave was never restarted after `trust`.

**Check the device itself, independent of the browser:**

```sh
python3 main.py read-mouse   # prints motion/button events; Ctrl-C to stop
```

If nothing prints while you move the puck, the problem is spacenavd/the
device, not the bridge — check `systemctl status spacenavd` and that
Blender/FreeCAD still see it.

**Movement feels wrong (inverted, or rotation twitchy).** `--mode camera`
flips the translate/rotate direction. `--translation-speed`,
`--rotation-speed`, and `--deadzone` are the other knobs (see `python3
main.py serve --help`). Full-scale deflection is assumed to be 350, which is
the device's own HID reporting limit and the correct default unless you've
customized `/etc/spnavrc` sensitivity.

**Camera jumps randomly, or keeps moving after you let go of the device.**
Both were real bugs, fixed — see "Two smoothness bugs found the hard way"
above. If either resurfaces (a different device model, say, with different
release behaviour than the SpaceMouse Compact this was tuned against), the
knob to look at first is `spacenav.Client._STALE_AFTER` (150ms) — raise it
if a legitimately *held* gesture on your device ever gets mistaken for
"gone quiet" and momentarily stutters to zero.

**Certificate expired / re-issuing.** `gen-certs` regenerates both CA and
leaf from scratch, which invalidates the previous trust entry along with it.
(`trust` checks the installed CA against the one on disk, not just its name,
so it will tell you when a re-run is genuinely needed and no-op when it
isn't.)
Re-run `trust` afterwards. In steady state only the leaf (825 days) needs
occasional renewal; `serve` regenerates it automatically when it's close to
expiring, but the *new* leaf is still signed by the *same* CA as long as you
don't also run `gen-certs`, so no `trust` re-run is needed for a routine
renewal.

## Scope / what's not implemented

- Firefox trust injection isn't wired up (Gecko uses a different mechanism,
  `cert_override.txt`, not NSS `certutil` trust flags) — Brave/Chrome only
  for now.
- No `-calibrate` step; defaults assume an uncustomized `/etc/spnavrc`
  (full-scale deflection = 350, the device's own HID limit). If you've tuned
  sensitivity in `/etc/spnavrc`, pass `--translation-speed`/`--rotation-speed`
  to compensate, or file the difference as a follow-up.
- Button mapping is available (`--buttons 0=fit,1=menu`) but only `fit` is
  meaningfully implemented end-to-end; `menu` sends the V3DK_MENU key, which
  Onshape may or may not act on.
- Single connection semantics were not stress-tested with multiple Onshape
  tabs open at once against the same bridge.
