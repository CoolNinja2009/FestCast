"""Dependency container: builds every service once and wires OBS events to them."""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import platform
import secrets
import sys
import time
import zipfile
from pathlib import Path

from ..config import Settings
from ..database.db import Database
from ..database.models import Camera, Layout, Presentation, Receiver
from ..obs.client import ObsTransport
from ..obs.controller import OBSController
from ..obs.mock import MockObsTransport
from ..services.audio import AudioService
from ..services.cameras import CameraService, cam_input_name
from ..services.layouts import LayoutService
from ..services.metrics import MetricsService, lan_ip
from .mdns import Mdns
from ..services.outputs import OutputService
from ..services.live import LiveService
from ..services.tally import TallyService
from ..services.presentations import PresentationService
from ..services.production import ProductionService
from ..services.workspace import WorkspaceService
from ..services.recording import RecordingService
from .events import EventBus, setup_logging

log = logging.getLogger("festcast")


_awake_proc = None


def keep_awake(on: bool) -> None:
    """Stop the PC sleeping or blanking the screen while FestCast runs (a sleeping PC ends the show).
    Windows: SetThreadExecutionState (per thread; FestCast's event loop thread lives for the whole run).
    macOS: `caffeinate` tied to this process. Linux: `systemd-inhibit` holding sleep/idle while this process lives."""
    global _awake_proc
    if sys.platform != "win32":
        import shutil
        import subprocess
        if not on:
            if _awake_proc and _awake_proc.poll() is None:
                _awake_proc.terminate()
            _awake_proc = None
            return
        pid = str(os.getpid())
        if sys.platform == "darwin" and shutil.which("caffeinate"):
            cmd = ["caffeinate", "-dims", "-w", pid]
        elif shutil.which("systemd-inhibit"):
            cmd = ["systemd-inhibit", "--what=sleep:idle", "--who=FestCast", "--why=Live show running",
                   "--mode=block", "sh", "-c", f"while kill -0 {pid} 2>/dev/null; do sleep 5; done"]
        else:
            log.warning("cannot block sleep on this system: switch off sleep / screen blanking by hand")
            return
        try:
            _awake_proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL)
            log.info("sleep blocked while FestCast runs (%s)", cmd[0])
        except OSError as e:
            log.warning("could not block sleep: %s", e)
        return
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x00000001, 0x00000002
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED if on else 0)
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
        log.info("Windows sleep %s", "blocked while FestCast runs" if on else "allowed again")
    except Exception:  # noqa: BLE001
        log.warning("could not change Windows sleep settings")


class Ctx:
    def __init__(self, settings: Settings, db: Database | None = None, bus: EventBus | None = None,
                 transport_factory=None) -> None:
        self.settings = settings
        self.bus = bus or EventBus()
        self.db = db or Database(settings.db_path)
        self.media_dir = settings.data_dir / "media"
        self.media_dir.mkdir(parents=True, exist_ok=True)
        # local_key authenticates OBS-side pages (/obs/*) - they run inside OBS on this machine
        self.local_key = self.db.get("local_key") or secrets.token_urlsafe(16)
        self.db.set("local_key", self.local_key)
        self.lan_ip = settings.public_host or lan_ip()
        self.mdns = Mdns(settings.mdns_name, self.lan_ip, settings.port)
        if transport_factory is None:
            if settings.simulation:
                transport_factory = MockObsTransport
            else:
                transport_factory = lambda cb: ObsTransport(settings.obs_host, settings.obs_port,  # noqa: E731
                                                            settings.obs_password, cb)
        self.obs = OBSController(self.bus, self.db, transport_factory)
        self.cameras = CameraService(self)
        self.layouts = LayoutService(self)
        self.production = ProductionService(self)
        self.recording = RecordingService(self)
        self.outputs = OutputService(self)
        self.live = LiveService(self)
        self.tally = TallyService(self)
        self.presentations = PresentationService(self)
        self.workspace = WorkspaceService(self)
        self.audio = AudioService(self)
        self.metrics = MetricsService(self)
        self.previews: dict[str, str] = {}
        self._tasks: list[asyncio.Task] = []

        self.obs.on_event("RecordStateChanged", self.recording.on_record_state)
        self.obs.on_event("InputVolumeMeters", self.audio.on_meters)
        self.obs.on_event("CurrentProgramSceneChanged", lambda d: self._scene_changed())
        self.obs.on_event("CurrentPreviewSceneChanged", lambda d: self._scene_changed())
        self.obs.on_event("StudioModeStateChanged", lambda d: self._scene_changed())
        self.obs.on_event("StreamStateChanged", lambda d: self.outputs.publish())
        self.obs.on_connect(self._resync)

    @property
    def public_base(self) -> str:
        """Base of phone/TV links: festcast.local when mDNS is announced, else the IP."""
        return f"http://{self.mdns.host or self.lan_ip}:{self.settings.port}"

    @property
    def ip_base(self) -> str:
        """The same links by IP, for devices that don't resolve .local names."""
        return f"http://{self.lan_ip}:{self.settings.port}"

    @property
    def https_port(self) -> int:
        return self.settings.https_port

    def https_base(self, ip: bool = False) -> str:
        """Base of the local phone camera page (self-signed HTTPS: phones confirm it once). Always the LAN IP, never
        the .local name: iPhones would not open https on the .local name (page stuck loading), but do on the IP
        (verified on iOS 27, 2026-11-20). `ip` is kept for callers; both give the same link."""
        return f"https://{self.lan_ip}:{self.settings.https_port}"

    async def _start_mdns(self) -> None:
        if await self.mdns.start():
            self.cameras.publish()
            self.outputs.publish()

    def _scene_changed(self) -> None:
        self.production.publish()
        self.layouts.publish()
        log.info("programme=%s preview=%s", self.obs.state.get("program_scene"), self.obs.state.get("preview_scene"))

    @property
    def own_obs(self) -> bool:
        s = self.settings
        return s.obs_autostart and not s.simulation and s.obs_host in ("127.0.0.1", "localhost", "::1")

    async def _launch_obs(self) -> None:
        from ..obs.launcher import ensure_obs
        s = self.settings
        try:
            r = await asyncio.to_thread(ensure_obs, s.obs_path, s.obs_profile, s.obs_collection,
                                        s.data_dir / "obs_session.json")
            log.info("OBS autostart: %s", r)
            if r.startswith("not found"):
                self.bus.notify("warning", "FestCast could not find OBS to start it. Start OBS yourself or set "
                                "FESTCAST_OBS_PATH in .env.", "obs")
        except Exception as e:  # noqa: BLE001
            log.exception("OBS autostart failed")
            self.bus.notify("warning", f"Could not start OBS: {e}", "obs")

    async def _check_collection(self) -> None:
        """FestCast's scenes belong in its own OBS scene collection. If OBS was opened another way, say so."""
        if not self.own_obs:
            return
        r = await self.obs.try_call("GetSceneCollectionList")
        cur = (r or {}).get("currentSceneCollectionName")
        if cur and cur != self.settings.obs_collection:
            self.bus.notify("warning", f"OBS is using your scene collection '{cur}', not '{self.settings.obs_collection}'"
                            ": FestCast's scenes will be added there. To keep your OBS clean, close OBS and restart "
                            "FestCast (it opens OBS itself).", "obs")

    async def _resync(self) -> None:
        """After (re)connecting to OBS: recreate anything missing. Existing sources are kept, so a live
        programme is not disturbed."""
        await self._check_collection()
        await self.production.ensure_standby()
        await self.cameras.sync_all()
        await self.layouts.sync_all()
        await self.audio.sync_all()
        await self.recording.on_obs_connect()
        await self.outputs.resume_if_wanted()
        self.production.publish()
        self.recording.publish()

    async def preview_loop(self) -> None:
        """Low-rate thumbnails via OBS screenshots: no extra WebRTC viewer, so phones upload only once."""
        while True:
            try:
                if self.obs.connected and self.bus._subs:
                    shots: dict[str, str] = {}
                    for c in self.cameras.cameras():
                        img = await self.obs.screenshot(cam_input_name(c.id), 400, 55)
                        if img:
                            shots[c.id] = img
                    for key in ("program_scene", "preview_scene"):
                        sc = self.obs.state.get(key)
                        if sc:
                            img = await self.obs.screenshot(sc, 1280, 80)   # sharp enough to read slide text
                            if img:
                                shots[key] = img
                    self.previews = shots
                    self.bus.publish("previews", shots)
            except Exception:  # noqa: BLE001
                log.exception("preview loop")
            await asyncio.sleep(self.settings.preview_interval)

    def start(self) -> None:
        setup_logging(self.bus, self.settings.data_dir / "logs")
        log.info("FestCast starting (%s) on %s", "SIMULATION" if self.settings.simulation else "live OBS",
                 self.public_base)
        self.started_at = time.time()
        keep_awake(True)
        if self.own_obs:
            self._tasks.append(asyncio.create_task(self._launch_obs(), name="obs-launch"))
        self.obs.start()
        self._loops ={"metrics": self.metrics.run, "standby": self.production.run, "previews": self.preview_loop,
                       "tally": self.tally.run}
        self._loop_tasks = {name: asyncio.create_task(f(), name=name) for name, f in self._loops.items()}
        self.restarts: dict[str, int] = {}
        self._tasks.append(asyncio.create_task(self._supervise(), name="supervisor"))
        self._tasks.append(asyncio.create_task(self._start_mdns(), name="mdns"))
        if self.outputs.want_programme:
            self.outputs.ensure_watchdog()
        self._tasks.append(asyncio.create_task(self.cameras.ensure_mtx(), name="camera-mediamtx"))
        self._tasks.append(asyncio.create_task(self.live.resume_if_wanted(), name="live-resume"))

    async def _supervise(self) -> None:
        """A long show must never lose a background loop to an unexpected bug: restart any that ends."""
        while True:
            await asyncio.sleep(5)
            for name, task in list(self._loop_tasks.items()):
                if task.done() and not task.cancelled():
                    err = task.exception()
                    self.restarts[name] = self.restarts.get(name, 0) + 1
                    log.error("background loop '%s' stopped (%r) - restarting", name, err)
                    self._loop_tasks[name] = asyncio.create_task(self._loops[name](), name=name)
            if self.obs._task is not None and self.obs._task.done() and not self.obs._task.cancelled():
                log.error("OBS connection loop stopped (%r) - restarting", self.obs._task.exception())
                self.restarts["obs"] = self.restarts.get("obs", 0) + 1
                self.obs.start()
            if self.outputs.want_programme or self.outputs.want_mtx:
                self.outputs.ensure_watchdog()

    async def stop(self) -> None:
        for t in [*self._tasks, *getattr(self, "_loop_tasks", {}).values()]:
            t.cancel()
        await asyncio.gather(*self._tasks, *getattr(self, "_loop_tasks", {}).values(), return_exceptions=True)
        await self.live.shutdown()
        await self.outputs.shutdown()
        await self.obs.stop()
        await self.mdns.stop()
        keep_awake(False)

    # -- diagnostics & backup ------------------------------------------------------------------------------
    def diagnostics_zip(self) -> bytes:
        """Support bundle without credentials, stream ids or invitation/receiver tokens."""
        buf = io.BytesIO()
        cams = [{"name": c.name, "slot": c.slot, "enabled": c.enabled, "resolution_target": c.resolution_target,
                 "live": {k: v for k, v in self.cameras.live.get(c.id, {}).items() if k != "raw"}}
                for c in self.cameras.cameras()]
        info = {"generated": time.time(), "python": sys.version, "platform": platform.platform(),
                "simulation": self.settings.simulation, "obs": {k: v for k, v in self.obs.state.items()},
                "cameras": cams, "recording": {k: v for k, v in self.recording.view().items()},
                "outputs": {"mediamtx": self.outputs.view()["mediamtx"],
                            "receivers": [{k: v for k, v in r.items() if k not in ("link", "ip_link")}
                                          for r in self.outputs.view()["receivers"]],
                            "live": self.live.view()},
                "metrics": self.metrics.latest, "notifications": list(self.bus.notifications)}
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("diagnostics.json", json.dumps(info, indent=2, default=str))
            logdir = self.settings.data_dir / "logs"
            for f in sorted(logdir.glob("festcast.jsonl*")):
                txt = f.read_text("utf-8", errors="replace")
                for secret in self._secrets():
                    txt = txt.replace(secret, "<redacted>")
                z.writestr(f"logs/{f.name}", txt)
        return buf.getvalue()

    def _secrets(self) -> list[str]:
        out = [self.local_key, self.settings.obs_password, self.settings.admin_password]
        with self.db.session() as s:
            out += [c.stream_id for c in s.query(Camera)] + [r.token for r in s.query(Receiver)]
            from ..database.models import Invitation, Production
            out += [i.token for i in s.query(Invitation)] + [p.vdo_password for p in s.query(Production)]
        out += self.live.secrets()
        return [x for x in out if x]

    def export_config(self) -> dict:
        with self.db.session() as s:
            layouts = [{"name": l.name, "elements": l.elements} for l in s.query(Layout)]
            pres = [{"name": p.name, "slides": len(p.slides)} for p in s.query(Presentation)]
        return {"festcast_config": 1, "exported": time.time(), "layouts": layouts, "presentations_info": pres,
                "settings": {k: self.db.get(k) for k in ("recording", "standby_rules", "standby_message")}}

    async def import_config(self, data: dict) -> dict:
        if data.get("festcast_config") != 1:
            raise ValueError("not a FestCast config export")
        n = 0
        for l in data.get("layouts", []):
            await self.layouts.create(l.get("name", "Imported"), l.get("elements", []))
            n += 1
        st = data.get("settings") or {}
        if st.get("recording"):
            self.recording.set_config(st["recording"])
        if st.get("standby_rules"):
            self.production.set_rules(st["standby_rules"])
        if st.get("standby_message"):
            self.db.set("standby_message", str(st["standby_message"])[:200])
        return {"layouts": n}


_ = Path
