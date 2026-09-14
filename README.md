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

```sh
python3 main.py serve
```

Leave it running, then open a Part Studio or Assembly (a document with a 3D
viewport — not the dashboard) in Brave. `-v` for verbose logging; it logs
each connection's handshake, so you'll see `3dcontroller created
client=Onshape` and then `navigation active` if everything is wired up.

### Run it in the background (systemd user service)

```sh
mkdir -p ~/.config/systemd/user
cp systemd/onshape-spacemouse-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now onshape-spacemouse-bridge
journalctl --user -u onshape-spacemouse-bridge -f   # logs
```

Not enabled automatically — run the commands above yourself when you're
happy it works via `serve` directly.

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

**Certificate expired / re-issuing.** `gen-certs` regenerates both CA and
leaf from scratch, which invalidates the previous trust entry along with it.
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
