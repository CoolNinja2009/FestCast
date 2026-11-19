"""OBS WebSocket v5 transport.

A small native-asyncio implementation of the documented protocol (Hello -> Identify -> Identified, op 6/7 requests,
op 5 events, op 8/9 batches) so no OBS call ever blocks the event loop. `MockObsTransport` implements the same
`call()` contract against an in-memory OBS model for simulation mode and tests.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import itertools
import json
import logging
from typing import Any, Awaitable, Callable

import websockets

log = logging.getLogger("festcast.obs")

# EventSubscription::All (general..vendors = bits 0-10) + InputVolumeMeters (1 << 16)
SUB_ALL = (1 << 11) - 1
SUB_METERS = 1 << 16


class ObsError(Exception):
    def __init__(self, request: str, code: int, comment: str = "") -> None:
        super().__init__(f"OBS {request} failed ({code}): {comment}")
        self.request, self.code, self.comment = request, code, comment


class ObsNotConnected(ObsError):
    def __init__(self, request: str = "") -> None:
        super().__init__(request, -1, "OBS is not connected")


EventCb = Callable[[str, dict], Awaitable[None] | None]


class ObsTransport:
    """Real OBS connection."""

    simulated = False

    def __init__(self, host: str, port: int, password: str, on_event: EventCb, timeout: float = 8.0) -> None:
        self.host, self.port, self.password = host, port, password
        self.on_event = on_event
        self.timeout = timeout
        self.ws: Any = None
        self._pending: dict[str, asyncio.Future] = {}
        self._ids = itertools.count(1)
        self._reader: asyncio.Task | None = None
        self.hello: dict = {}
        self.closed = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self.ws is not None and not self.closed.is_set()

    async def connect(self) -> None:
        self.closed = asyncio.Event()
        ws = await asyncio.wait_for(
            websockets.connect(f"ws://{self.host}:{self.port}", max_size=64 * 1024 * 1024,
                               subprotocols=["obswebsocket.json"], open_timeout=5, ping_interval=10),
            timeout=6)
        hello = json.loads(await asyncio.wait_for(ws.recv(), 5))
        if hello.get("op") != 0:
            await ws.close()
            raise ObsError("Hello", -2, "unexpected handshake")
        self.hello = hello["d"]
        ident: dict[str, Any] = {"rpcVersion": 1, "eventSubscriptions": SUB_ALL | SUB_METERS}
        auth = self.hello.get("authentication")
        if auth:
            if not self.password:
                await ws.close()
                raise ObsError("Identify", 4009, "OBS requires a WebSocket password (set FESTCAST_OBS_PASSWORD)")
            secret = base64.b64encode(hashlib.sha256((self.password + auth["salt"]).encode()).digest()).decode()
            ident["authentication"] = base64.b64encode(
                hashlib.sha256((secret + auth["challenge"]).encode()).digest()).decode()
        await ws.send(json.dumps({"op": 1, "d": ident}))
        try:
            msg = json.loads(await asyncio.wait_for(ws.recv(), 5))
        except websockets.ConnectionClosed as e:
            raise ObsError("Identify", e.rcvd.code if e.rcvd else -3,
                           "authentication failed - check the OBS WebSocket password") from e
        if msg.get("op") != 2:
            await ws.close()
            raise ObsError("Identify", -3, "identify rejected")
        self.ws = ws
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                op, d = msg.get("op"), msg.get("d", {})
                if op == 7:
                    fut = self._pending.pop(d.get("requestId"), None)
                    if fut and not fut.done():
                        fut.set_result(d)
                elif op == 5:
                    try:
                        r = self.on_event(d.get("eventType", ""), d.get("eventData") or {})
                        if asyncio.iscoroutine(r):
                            await r
                    except Exception:
                        log.exception("OBS event handler failed")
        except websockets.ConnectionClosed:
            pass
        except Exception:
            log.exception("OBS reader crashed")
        finally:
            self.ws = None
            self.closed.set()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ObsNotConnected())
            self._pending.clear()

    async def call(self, request: str, data: dict | None = None) -> dict:
        if not self.connected:
            raise ObsNotConnected(request)
        rid = str(next(self._ids))
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        payload: dict[str, Any] = {"requestType": request, "requestId": rid}
        if data:
            payload["requestData"] = data
        await self.ws.send(json.dumps({"op": 6, "d": payload}))
        try:
            d = await asyncio.wait_for(fut, self.timeout)
        finally:
            self._pending.pop(rid, None)
        st = d.get("requestStatus", {})
        if not st.get("result"):
            raise ObsError(request, st.get("code", 0), st.get("comment", ""))
        return d.get("responseData") or {}

    async def close(self) -> None:
        if self.ws is not None:
            await self.ws.close()
        if self._reader:
            await asyncio.gather(self._reader, return_exceptions=True)
