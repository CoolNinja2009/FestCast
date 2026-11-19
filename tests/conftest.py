"""Shared fixtures: every test gets its own data dir, a simulated OBS (MockObsTransport) and helpers to run
coroutines on the app's event loop (TestClient's portal)."""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config import Settings  # noqa: E402
from backend.core.context import Ctx  # noqa: E402
from backend.main import create_app  # noqa: E402

LAN = "192.168.1.10"
LOCAL = ("127.0.0.1", 50000)
REMOTE = ("192.168.0.50", 5000)


def make_settings(tmp_path: Path, **kw) -> Settings:
    base = dict(data_dir=tmp_path / "data", simulation=True, public_host=LAN, port=8000, admin_password="",
                obs_password="", mediamtx_path="", metrics_interval=60.0, preview_interval=60.0)
    base.update(kw)
    s = Settings(_env_file=None, **base)
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s


def wait_for(pred, timeout: float = 5.0, msg: str = "condition") -> None:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {msg}")


class Env:
    def __init__(self, tmp_path: Path, **settings_kw) -> None:
        self.tmp = tmp_path
        self.settings = make_settings(tmp_path, **settings_kw)
        self.ctx = Ctx(self.settings)
        self.rec_root = tmp_path / "rec"
        # never let the mock / recording touch the real ~/Videos
        self.ctx.recording.set_config({"root": str(self.rec_root), "auto_start": False})
        self.connects: list[float] = []

        async def _hook() -> None:      # runs after Ctx._resync, so resync is complete when this fires
            self.connects.append(time.time())
        self.ctx.obs.on_connect(_hook)
        self.app = create_app(self.settings, self.ctx)
        self.client = TestClient(self.app, client=LOCAL)
        self._remotes: list[TestClient] = []

    # -- lifecycle ---------------------------------------------------------------
    def __enter__(self) -> "Env":
        self.client.__enter__()
        self.wait_connects(1)

        async def _stop_standby_loop() -> None:   # tests drive ProductionService.evaluate() themselves
            for t in self.ctx._tasks:
                if "ProductionService.run" in t.get_coro().__qualname__:
                    t.cancel()
        self.run(_stop_standby_loop)
        return self

    def __exit__(self, *exc) -> None:
        try:
            self.client.__exit__(*exc)
        finally:
            for h in list(logging.getLogger("festcast").handlers):
                h.close()
                logging.getLogger("festcast").removeHandler(h)
            self.ctx.db.engine.dispose()

    # -- helpers -----------------------------------------------------------------
    @property
    def mock(self):
        return self.ctx.obs.transport

    def run(self, fn, *args):
        """Run an async callable on the app's event loop."""
        return self.client.portal.call(fn, *args)

    def obs(self, req: str, data: dict | None = None) -> dict:
        """Call the mock OBS directly (as if done in OBS itself, not via FestCast)."""
        return self.run(self.mock.call, req, data)

    def drop(self) -> None:
        """Simulate OBS crashing (on the loop thread, so waiters wake up)."""
        self.run(self._drop)

    async def _drop(self) -> None:
        self.mock.drop()

    def wait_connects(self, n: int, timeout: float = 6.0) -> None:
        wait_for(lambda: len(self.connects) >= n and self.ctx.obs.connected, timeout, f"OBS connect #{n}")

    def remote(self) -> TestClient:
        c = TestClient(self.app, client=REMOTE)
        c.portal = self.client.portal          # share the app's event loop
        self._remotes.append(c)
        return c

    def scene_names(self) -> list[str]:
        return [s["sceneName"] for s in self.mock.scenes]

    def scene(self, name: str) -> dict:
        return next(s for s in self.mock.scenes if s["sceneName"] == name)

    def create_production(self, cameras: int = 3, name: str = "Fest") -> dict:
        r = self.client.post("/api/production", json={"name": name, "cameras": cameras})
        assert r.status_code == 200, r.text
        return r.json()

    def cam_ids(self) -> list[str]:
        return [c.id for c in self.ctx.cameras.cameras()]

    def report(self, cid: str, stats: dict | None, key: str | None = None, client: TestClient | None = None):
        return (client or self.client).post(f"/obs/cam/{cid}/stats", params={"k": key or self.ctx.local_key},
                                            json={"stats": stats})


LIVE_1080 = {"abc": {"Resolution": "1920 x 1080", "_FPS": 30, "video_bitrate_kbps": 5800}}
LIVE_720 = {"abc": {"Resolution": "1280 x 720", "_FPS": 30, "video_bitrate_kbps": 2500}}


@pytest.fixture
def env(tmp_path):
    with Env(tmp_path) as e:
        yield e


@pytest.fixture
def env_factory(tmp_path):
    made: list[Env] = []

    def _make(**kw) -> Env:
        e = Env(tmp_path / f"e{len(made)}", **kw)
        e.__enter__()
        made.append(e)
        return e
    yield _make
    for e in made:
        e.__exit__(None, None, None)


@pytest.fixture
def bare_ctx(tmp_path):
    """A Ctx without a running app (for sync service-level tests)."""
    c = Ctx(make_settings(tmp_path))
    yield c
    c.db.engine.dispose()
