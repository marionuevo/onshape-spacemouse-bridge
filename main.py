#!/usr/bin/env python3
"""CLI for onshape-spacemouse-bridge.

Subcommands:
  serve       run the bridge (discovery + WAMP WebSocket on 127.51.68.120:8181)
  gen-certs   (re)generate the local CA + leaf certificate
  trust       inject the CA into Brave/Chrome's shared NSS trust store
  read-mouse  print spacenavd events -- sanity-check the device before
              troubleshooting the browser side
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from onshape_spacemouse_bridge import certs, nav, server


def cmd_serve(args: argparse.Namespace) -> None:
    cert_dir = Path(args.cert_dir)
    certs.ensure(cert_dir)
    if not certs.is_trusted_chromium():
        logging.warning(
            "CA not found in %s -- Brave/Chrome will show a certificate warning "
            "or fail the discovery request silently. Run: %s trust",
            certs.NSSDB, sys.argv[0],
        )

    config = nav.Config(
        mode=args.mode,
        translation_speed=args.translation_speed,
        rotation_speed=args.rotation_speed,
        deadzone=args.deadzone,
    )
    buttons = server.parse_buttons(args.buttons) if args.buttons else {}
    server.run(
        cert_dir, host=args.host, port=args.port, config=config,
        frame_rate=args.frame_rate, buttons=buttons,
    )


def cmd_gen_certs(args: argparse.Namespace) -> None:
    cert_dir = Path(args.cert_dir)
    certs.generate(cert_dir)
    print(f"wrote CA + leaf to {cert_dir}")
    print("run 'trust' next to install the CA into Brave/Chrome.")


def cmd_trust(args: argparse.Namespace) -> None:
    cert_dir = Path(args.cert_dir)
    if not (cert_dir / "ca.pem").exists():
        print(f"no certificate at {cert_dir} -- run 'gen-certs' first", file=sys.stderr)
        sys.exit(1)

    running = certs.browsers_running()
    if running and not args.force:
        print(f"these look like they're running: {', '.join(running)}")
        print("NSS reads its trust store at startup -- close them first, then re-run this,")
        print("or pass --force and restart the browser yourself afterwards.")
        sys.exit(1)

    certs.trust_chromium(cert_dir)
    print(f"installed '{certs.CA_NAME}' into {certs.NSSDB}")
    print("(re)start Brave and open an Onshape document to test.")


def cmd_read_mouse(args: argparse.Namespace) -> None:
    import time

    from onshape_spacemouse_bridge.spacenav import Client

    c = Client()
    print(f"connected: {c.socket_path}")
    try:
        while True:
            for ev in c.poll_events():
                print(ev)
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        c.close()


def main() -> None:
    p = argparse.ArgumentParser(prog="onshape-spacemouse-bridge")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    default_cert_dir = str(certs.DEFAULT_DIR)
    # -v/--verbose also accepted after the subcommand (e.g. "serve -v"), not
    # just before it.
    verbose_parent = argparse.ArgumentParser(add_help=False)
    verbose_parent.add_argument("-v", "--verbose", action="store_true")

    ps = sub.add_parser("serve", help="run the bridge", parents=[verbose_parent])
    ps.add_argument("--host", default=server.DEFAULT_HOST)
    ps.add_argument("--port", type=int, default=server.DEFAULT_PORT)
    ps.add_argument("--cert-dir", default=default_cert_dir)
    ps.add_argument("--frame-rate", type=int, default=60)
    ps.add_argument("--mode", choices=["object", "camera"], default="object",
                     help="object (default): push right, the MODEL goes right, matching Onshape's usual feel")
    ps.add_argument("--translation-speed", type=float, default=0.9)
    ps.add_argument("--rotation-speed", type=float, default=1.6)
    ps.add_argument("--deadzone", type=float, default=0.06)
    ps.add_argument("--buttons", default="", help="e.g. 0=fit,1=menu")
    ps.set_defaults(func=cmd_serve)

    pg = sub.add_parser("gen-certs", help="(re)generate the local CA + leaf")
    pg.add_argument("--cert-dir", default=default_cert_dir)
    pg.set_defaults(func=cmd_gen_certs)

    pt = sub.add_parser("trust", help="inject the CA into Brave/Chrome's NSS store")
    pt.add_argument("--cert-dir", default=default_cert_dir)
    pt.add_argument("--force", action="store_true")
    pt.set_defaults(func=cmd_trust)

    pr = sub.add_parser("read-mouse", help="print spacenavd events (Ctrl-C to stop)")
    pr.set_defaults(func=cmd_read_mouse)

    args = p.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args.func(args)


if __name__ == "__main__":
    main()
