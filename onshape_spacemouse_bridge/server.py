"""aiohttp glue: the HTTPS discovery endpoint and the WAMP WebSocket that
3DconnexionJS expects, both on 127.51.68.120:8181 -- hardcoded in the client
library (3dconnexion.js:98,103) and not configurable from the page.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from aiohttp import WSMsgType, web

from . import bridge as bridge_mod
from . import drive as drive_mod
from . import nav, wamp
from .spacenav import Client as SpacenavClient

log = logging.getLogger("server")

DEFAULT_HOST = "127.51.68.120"
DEFAULT_PORT = 8181
NLPROXY_VERSION = "1.4.8.21486"  # mirrors what the real NL-Proxy reports; some clients may gate on it
SERVER_IDENT = "onshape-spacemouse-bridge Copyright 2026"

_VALID_BUTTON_ACTIONS = {"none", "fit", "menu", "dominant-axis", "rotation-lock"}

_STATUS_PAGE = """<!doctype html><meta charset=utf-8>
<title>onshape-spacemouse-bridge</title>
<style>body{font:14px system-ui;margin:3rem auto;max-width:34rem;line-height:1.6}
code{background:#f4f4f5;padding:.15em .4em;border-radius:3px}</style>
<h1>onshape-spacemouse-bridge</h1>
<p>Running. If you reached this page without a certificate warning, browser
trust is correctly installed.</p>
<p>Discovery endpoint: <code>/3dconnexion/nlproxy</code></p>"""


def parse_buttons(spec: str) -> dict[int, str]:
    """Parse a mapping such as '0=fit,1=menu'."""
    out: dict[int, str] = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(f"button mapping {part!r}: want id=action")
        id_str, name = part.split("=", 1)
        action = name.strip()
        if action not in _VALID_BUTTON_ACTIONS:
            raise ValueError(f"button mapping {part!r}: unknown action {action!r}")
        out[int(id_str.strip())] = action
    return out


def _set_cors(resp: web.StreamResponse, request: web.Request) -> None:
    origin = request.headers.get("Origin", "*")
    resp.headers["Access-Control-Allow-Origin"] = origin
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    resp.headers["Vary"] = "Origin"


async def _discovery(request: web.Request) -> web.StreamResponse:
    if not _origin_allowed(request):
        log.warning("rejected discovery origin=%s (not in --allowed-origins)",
                    request.headers.get("Origin", "-"))
        return web.Response(status=403, text="origin not allowed")
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
        _set_cors(resp, request)
        return resp
    log.info("discovery origin=%s", request.headers.get("Origin", "-"))
    resp = web.json_response({"port": request.app["port"], "version": NLPROXY_VERSION})
    _set_cors(resp, request)
    return resp


async def _close_websockets(app: web.Application) -> None:
    """Close every live WebSocket so the server can actually shut down."""
    for ws in set(app["websockets"]):
        try:
            await ws.close(code=1001, message=b"server shutting down")
        except Exception:  # noqa: BLE001 -- shutting down regardless
            pass
    app["websockets"].clear()


async def _root(request: web.Request) -> web.StreamResponse:
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return await _wamp_socket(request)
    return web.Response(text=_STATUS_PAGE, content_type="text/html")


def _origin_allowed(request: web.Request) -> bool:
    """Any page the browser loads can reach this socket, exactly as it can
    reach the real 3Dconnexion driver -- there is no same-origin protection
    on a WebSocket, and 3dconnexion.js is served from whatever site is using
    it, so an allowlist cannot be the default without breaking unknown
    clients. `focus` limits what an unwanted connection can do (it only ever
    drives its own page's camera, and only while that page says it has
    focus), but --allowed-origins is there for locking this down to Onshape.
    """
    allowed = request.app["allowed_origins"]
    if not allowed:
        return True
    return request.headers.get("Origin", "") in allowed


async def _wamp_socket(request: web.Request) -> web.StreamResponse:
    if not _origin_allowed(request):
        log.warning("rejected websocket origin=%s (not in --allowed-origins)",
                    request.headers.get("Origin", "-"))
        return web.Response(status=403, text="origin not allowed")

    ws = web.WebSocketResponse(protocols=[wamp.SUBPROTOCOL])
    await ws.prepare(request)

    origin = request.headers.get("Origin", "-")
    # aiohttp renamed WebSocketResponse.protocol -> ws_protocol at some point;
    # tolerate either so this doesn't crash the connection on a version skew.
    negotiated = getattr(ws, "ws_protocol", None) or getattr(ws, "protocol", None)
    log.info("client connected origin=%s subprotocol=%s", origin, negotiated)

    # aiohttp's shutdown does not close live WebSockets -- it waits for
    # their handlers to return, and `async for msg in ws` never does while
    # the browser keeps the connection open. With an Onshape tab open, a
    # `systemctl --user restart` therefore sat through the full stop timeout
    # and ended in SIGKILL (90s, observed). Track the sockets so on_shutdown
    # can close them.
    request.app["websockets"].add(ws)

    session = wamp.Session(ws, log)
    device: SpacenavClient = request.app["device"]
    config: nav.Config = request.app["config"]
    frame_rate: int = request.app["frame_rate"]
    buttons: dict[int, str] = request.app["buttons"]

    drive_task: Optional[asyncio.Task] = None

    async def stop_driving():
        """Cancel the running drive loop and wait for it to finish.

        Waiting matters. The loop's own finally sends a last motion=false,
        so returning before it has unwound leaves that write racing the
        socket teardown; and not retrieving the task at all turns any error
        raised on the way out into an unretrieved-task-exception warning at
        every single disconnect.
        """
        nonlocal drive_task
        task, drive_task = drive_task, None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def on_ready(controller):
        nonlocal drive_task
        # Never leave a previous loop running. Two drive loops on one
        # connection both write view.affine, on different controller topics
        # and with independently stale ideas of focus.
        await stop_driving()
        drive_task = asyncio.ensure_future(
            drive_mod.drive(device, controller, config, frame_rate, buttons)
        )

    handler = bridge_mod.Bridge(on_ready=on_ready, on_release=stop_driving)

    try:
        await session.welcome(SERVER_IDENT)
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                await session.dispatch(handler, msg.data)
            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE, WSMsgType.CLOSING):
                break
    finally:
        await stop_driving()
        request.app["websockets"].discard(ws)
        log.info("client disconnected")

    return ws


def build_app(
    device: SpacenavClient,
    config: nav.Config,
    port: int = DEFAULT_PORT,
    frame_rate: int = 60,
    buttons: Optional[dict[int, str]] = None,
    allowed_origins: Optional[set] = None,
) -> web.Application:
    app = web.Application()
    app["device"] = device
    app["config"] = config
    app["port"] = port
    app["frame_rate"] = frame_rate
    app["buttons"] = buttons or {}
    app["allowed_origins"] = allowed_origins or set()
    app["websockets"] = set()
    app.on_shutdown.append(_close_websockets)
    app.router.add_route("GET", "/3dconnexion/nlproxy", _discovery)
    app.router.add_route("OPTIONS", "/3dconnexion/nlproxy", _discovery)
    app.router.add_route("GET", "/", _root)
    return app


def run(
    cert_dir: Path,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    config: Optional[nav.Config] = None,
    frame_rate: int = 60,
    buttons: Optional[dict[int, str]] = None,
    allowed_origins: Optional[set] = None,
) -> None:
    import ssl

    config = config or nav.Config()
    device = SpacenavClient()
    log.info("connected to spacenavd socket=%s", device.socket_path)

    app = build_app(device, config, port, frame_rate, buttons, allowed_origins)
    if allowed_origins:
        log.info("restricting to origins=%s", ", ".join(sorted(allowed_origins)))

    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_ctx.load_cert_chain(certfile=str(cert_dir / "fullchain.pem"), keyfile=str(cert_dir / "leaf-key.pem"))

    log.info("listening host=%s port=%s", host, port)
    # shutdown_timeout is a backstop only: _close_websockets should have
    # ended every connection well before it expires.
    web.run_app(app, host=host, port=port, ssl_context=ssl_ctx, print=None,
                shutdown_timeout=5.0)
