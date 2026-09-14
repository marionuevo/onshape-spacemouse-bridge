"""The subset of WAMP v1 (https://wamp-proto.org, v1 draft) spoken by
3Dconnexion's 3DconnexionJS client library (bundled in Onshape's page).

Two quirks matter:

1. The client never sends HELLO. The server speaks first with WELCOME.
2. WAMP v1 has no server-initiated CALL. The driver smuggles a CALL message
   inside the payload of an EVENT published to the controller topic; the
   page unwraps it (3dconnexion.js:onEvent) and answers with a *bare*
   CALLRESULT/CALLERROR carrying our call id. See Session.call_client.

Handshake, captured against a live Onshape session:

    rece [0,"<sid>",1,"Nl-Proxy v1.4.0..."]                          WELCOME
    send [1,"3dx_rpc","wss://127.51.68.120/3dconnexion#"]            PREFIX
    send [1,"3dconnexion","wss://127.51.68.120/3dconnexion"]         PREFIX
    send [1,"self","https://.../web_threejs.html"]                   PREFIX
    send [2,"<id>","3dx_rpc:create","3dconnexion:3dmouse","0.3.11"]  CALL
    rece [3,"<id>",{"connexion":"..."}]                              CALLRESULT
    send [2,"<id>","3dx_rpc:create","3dconnexion:3dcontroller","...",{...}]
    rece [3,"<id>",{"instance":...}]
    send [5,"3dconnexion:3dcontroller/<instance>"]                   SUBSCRIBE
    send [2,"<id>","3dx_rpc:update","3dconnexion:3dcontroller/<id>",{"focus":true}]
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import string
from typing import Any, Optional, Protocol

TYPE_WELCOME = 0
TYPE_PREFIX = 1
TYPE_CALL = 2
TYPE_CALLRESULT = 3
TYPE_CALLERROR = 4
TYPE_SUBSCRIBE = 5
TYPE_UNSUBSCRIBE = 6
TYPE_PUBLISH = 7
TYPE_EVENT = 8

SUBPROTOCOL = "wamp"

_ALPHABET = string.ascii_letters + string.digits


def _rand_id(n: int = 16) -> str:
    return "".join(random.choices(_ALPHABET, k=n))


class CallError(Exception):
    """The page answered a server-to-client CALL with a CALLERROR -- normal
    for a property it doesn't implement. Treat as "unsupported", not fatal.
    """

    def __init__(self, uri: str, desc: str):
        super().__init__(f"{uri}: {desc}")
        self.uri = uri
        self.desc = desc


def is_unsupported(exc: BaseException) -> bool:
    return isinstance(exc, CallError)


class Handler(Protocol):
    async def on_call(self, session: "Session", proc_uri: str, args: list) -> Any: ...
    async def on_subscribe(self, session: "Session", topic: str) -> None: ...


class Session:
    """Wraps one WebSocket connection speaking WAMP v1.

    Transport-agnostic on purpose: it only needs an object with an async
    `send_str`, so it works unmodified against aiohttp's WebSocketResponse.
    """

    def __init__(self, ws, log: Optional[logging.Logger] = None):
        self.ws = ws
        self.log = log or logging.getLogger("wamp")
        self.id = _rand_id()
        self._prefixes: dict[str, str] = {}
        self._pending: dict[str, "asyncio.Future"] = {}
        self._send_lock = asyncio.Lock()

    async def _send(self, kind: int, *args: Any) -> None:
        msg = json.dumps([kind, *args])
        self.log.debug("wamp send %s", msg)
        async with self._send_lock:
            await self.ws.send_str(msg)

    async def welcome(self, server_ident: str) -> None:
        await self._send(TYPE_WELCOME, self.id, 1, server_ident)

    async def reply(self, call_id: str, result: Any) -> None:
        await self._send(TYPE_CALLRESULT, call_id, result)

    async def reply_error(self, call_id: str, uri: str, desc: str) -> None:
        await self._send(TYPE_CALLERROR, call_id, uri, desc)

    def register_prefix(self, prefix: str, uri: str) -> None:
        self._prefixes[prefix] = uri

    def resolve(self, uri: str) -> str:
        """Expand a CURIE using the prefixes the client registered.

        The expansion is plain concatenation: prefix "3dconnexion" ->
        "wss://127.51.68.120/3dconnexion" and CURIE
        "3dconnexion:3dcontroller/7" become
        "wss://127.51.68.120/3dconnexion3dcontroller/7" -- no separating
        slash. That is what the real driver produces; don't "fix" it.
        """
        if ":" not in uri:
            return uri
        prefix, rest = uri.split(":", 1)
        base = self._prefixes.get(prefix)
        if base is None:
            return uri
        return base + rest

    async def cast_client(self, topic: str, proc_uri: str, *args: Any) -> None:
        """Like call_client, but does not wait for the page's reply.

        The wire message is identical; only whether we block for the answer
        differs. Use this on a latency-sensitive hot path where the caller
        doesn't need the result: awaiting every single WAMP round trip in
        strict sequence is what actually limits how smooth navigation can
        feel, far more than device polling or the matrix math. A CALLERROR
        that would have come back is simply never seen -- the same outcome
        call_client callers already tolerate for optional properties.

        Safe with respect to ordering: WebSocket delivers messages in order
        and the page's JS handles them one at a time, so a write cast here
        is still guaranteed to be applied before any read sent afterwards is
        processed, even though we don't wait around to confirm it.
        """
        call_id = _rand_id()
        inner = [TYPE_CALL, call_id, proc_uri, "", *args]
        await self._send(TYPE_EVENT, topic, inner)

    async def call_client(self, topic: str, proc_uri: str, *args: Any, timeout: float = 5.0) -> Any:
        """Server-to-client RPC via the reverse-RPC-in-EVENT trick: WAMP v1
        has no server-initiated CALL, so we publish an EVENT to the
        controller topic whose payload IS a CALL message, and wait for the
        page's bare CALLRESULT/CALLERROR reply.
        """
        call_id = _rand_id()
        inner = [TYPE_CALL, call_id, proc_uri, "", *args]

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[call_id] = fut
        try:
            await self._send(TYPE_EVENT, topic, inner)
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(call_id, None)

    def _resolve_pending(self, call_id: str, result: Any = None, error: Optional[CallError] = None) -> None:
        fut = self._pending.get(call_id)
        if fut is None or fut.done():
            return
        if error is not None:
            fut.set_exception(error)
        else:
            fut.set_result(result)

    async def dispatch(self, handler: Handler, raw: str) -> None:
        self.log.debug("wamp recv %s", raw)
        try:
            arr = json.loads(raw)
            if not isinstance(arr, list) or not arr:
                raise ValueError("empty or non-array message")
            kind = int(arr[0])
            elements = arr[1:]
        except (ValueError, TypeError) as e:
            self.log.warning("wamp: undecodable frame: %s", e)
            return

        if kind == TYPE_PREFIX:
            if len(elements) >= 2:
                self.register_prefix(elements[0], elements[1])
                self.log.debug("wamp prefix %s -> %s", elements[0], elements[1])

        elif kind == TYPE_CALL:
            if len(elements) < 2:
                return
            call_id, proc_uri = elements[0], elements[1]
            resolved = self.resolve(proc_uri)
            try:
                result = await handler.on_call(self, resolved, elements[2:])
            except Exception as e:  # noqa: BLE001 -- becomes a CALLERROR, not fatal
                await self.reply_error(call_id, f"{proc_uri}#generic", str(e))
                return
            await self.reply(call_id, result)

        elif kind == TYPE_SUBSCRIBE:
            if elements:
                await handler.on_subscribe(self, elements[0])

        elif kind == TYPE_CALLRESULT:
            if elements:
                self._resolve_pending(elements[0], result=elements[1] if len(elements) > 1 else None)

        elif kind == TYPE_CALLERROR:
            if elements:
                uri = elements[1] if len(elements) > 1 else ""
                desc = elements[2] if len(elements) > 2 else ""
                self._resolve_pending(elements[0], error=CallError(uri, desc))

        else:
            self.log.debug("wamp: unhandled message type %s", kind)
