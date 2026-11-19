"""In-process event bus + operator notifications + structured log ring.

Everything the dashboard sees live flows through here and out of the /api/ws WebSocket.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Awaitable, Callable

Handler = Callable[[str, dict], Awaitable[None] | None]

log = logging.getLogger("festcast")


class EventBus:
    def __init__(self) -> None:
        self._subs: list[asyncio.Queue] = []
        self._handlers: dict[str, list[Handler]] = {}
        self.notifications: deque[dict] = deque(maxlen=200)
        self.logs: deque[dict] = deque(maxlen=1000)

    # -- websocket fan-out -------------------------------------------------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subs.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subs:
            self._subs.remove(q)

    def publish(self, topic: str, data: Any) -> None:
        msg = {"topic": topic, "data": data, "ts": time.time()}
        for q in list(self._subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:  # slow client: drop oldest
                try:
                    q.get_nowait()
                    q.put_nowait(msg)
                except Exception:
                    pass
        for h in self._handlers.get(topic, []):
            try:
                r = h(topic, data)
                if asyncio.iscoroutine(r):
                    asyncio.ensure_future(r)
            except Exception:
                log.exception("event handler failed for %s", topic)

    def on(self, topic: str, handler: Handler) -> None:
        self._handlers.setdefault(topic, []).append(handler)

    # -- operator notifications -------------------------------------------
    def notify(self, level: str, message: str, source: str = "system", **extra: Any) -> None:
        n = {"id": f"{time.time_ns()}", "ts": time.time(), "level": level, "message": message, "source": source, **extra}
        self.notifications.appendleft(n)
        self.publish("notification", n)
        getattr(log, "warning" if level == "warning" else ("error" if level == "error" else "info"))(
            message, extra={"source": source})


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        d = {"ts": round(record.created, 3), "level": record.levelname.lower(), "logger": record.name,
             "msg": record.getMessage()}
        if getattr(record, "source", None):
            d["source"] = record.source
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info)
        return json.dumps(d, ensure_ascii=False)


class RingHandler(logging.Handler):
    def __init__(self, bus: EventBus) -> None:
        super().__init__(logging.INFO)
        self.bus = bus
        self.fmt = JsonFormatter()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.bus.logs.append(json.loads(self.fmt.format(record)))
        except Exception:
            pass


def setup_logging(bus: EventBus, log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("festcast")
    root.setLevel(logging.INFO)
    root.propagate = False
    for h in list(root.handlers):
        root.removeHandler(h)
    fh = RotatingFileHandler(log_dir / "festcast.jsonl", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(JsonFormatter())
    root.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
    root.addHandler(sh)
    root.addHandler(RingHandler(bus))
