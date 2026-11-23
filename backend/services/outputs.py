"""Programme output: OBS --WHIP--> MediaMTX --WHEP--> TV browser receivers, plus HDMI projectors.

MediaMTX endpoints verified in its docs (publish: /<path>/whip, read: /<path>/whep). Its ports (WebRTC HTTP, ICE
UDP/TCP, API) are chosen automatically at start: preferred 8889 / 8189 / 9997, else the next free ones.
MediaMTX only accepts requests from this PC. TV receivers never talk to MediaMTX's HTTP API directly: their WHEP
offer goes to FestCast (/api/public/tv/<token>/whep), which checks the receiver token and forwards it. The media then
flows directly MediaMTX -> TV over the ICE port. Revoking a receiver makes its token useless immediately."""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from ..database.models import Receiver
from ..obs.client import ObsError

if TYPE_CHECKING:
    from ..core.context import Ctx

log = logging.getLogger("festcast.outputs")


class OutputService:
    def __init__(self, ctx: "Ctx") -> None:
        self.ctx = ctx
        self.proc: asyncio.subprocess.Process | None = None
        self.mtx_log: deque[str] = deque(maxlen=200)
        self.mtx_state = "stopped"     # stopped | starting | running | crashed | missing
        self.want_mtx = False
        self.live: dict[str, dict] = {}      # receiver id -> runtime
        self.mtx_stats: dict[str, Any] = {}
        self._saved_service: dict | None = None
        self._watch: asyncio.Task | None = None
        # TV output should be on (survives FestCast restarts via the DB): the watchdog keeps it alive
        self.want_programme = bool(ctx.db.get("tv_output_on"))
        self._heal_backoff = 5.0
        self._next_heal = 0.0
        self._not_ready_since: float | None = None
        self.heals = 0
        self.adopted_pid: int | None = None   # MediaMTX left running by a previous FestCast, still serving TVs
        self.ports = {"webrtc": ctx.settings.mediamtx_webrtc_port, "ice": 8189, "api": 9997, "rtsp": 8554}

    # -- receivers ----------------------------------------------------------------------------------
    def receivers(self) -> list[Receiver]:
        with self.ctx.db.session() as s:
            return list(s.query(Receiver).filter(Receiver.revoked.is_(False)).order_by(Receiver.created_at))

    def add(self, name: str, kind: str = "webrtc", monitor_index: int | None = None) -> Receiver:
        if kind not in ("webrtc", "hdmi"):
            raise ValueError("kind must be webrtc or hdmi")
        with self.ctx.db.session() as s:
            r = Receiver(name=(name or "TV").strip()[:80], kind=kind, token=self._short_code(s),
                         monitor_index=monitor_index)
            s.add(r)
        log.info("receiver added %s (%s)", r.name, kind)
        self.publish()
        return r

    def rename(self, rid: str, name: str) -> None:
        with self.ctx.db.session() as s:
            r = s.get(Receiver, rid)
            if not r:
                raise KeyError(rid)
            r.name = name.strip()[:80] or r.name
        self.publish()

    async def revoke(self, rid: str) -> None:
        with self.ctx.db.session() as s:
            r = s.get(Receiver, rid)
            if not r:
                raise KeyError(rid)
            r.revoked = True
            kind = r.kind
        self.live.pop(rid, None)
        log.info("receiver %s revoked", rid)
        self.publish()

    # TV links are typed on a TV remote, so the code is short and has no look-alike characters (0/o, 1/l/i).
    CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"

    def _short_code(self, s, length: int = 6) -> str:
        while True:
            code = "".join(secrets.choice(self.CODE_ALPHABET) for _ in range(length))
            if not s.query(Receiver).filter(Receiver.token == code).first():
                return code

    def by_token(self, token: str) -> Receiver | None:
        with self.ctx.db.session() as s:
            r = s.query(Receiver).filter(Receiver.token == token).first()
            if not r and token != token.strip().lower():  # TV keyboards like to capitalise
                r = s.query(Receiver).filter(Receiver.token == token.strip().lower()).first()
            return r if r and not r.revoked else None

    async def whep(self, token: str, offer_sdp: str) -> tuple[int, str, str | None]:
        """Forward a receiver's WHEP offer to MediaMTX (token-checked). Returns (status, body, session path)."""
        if not self.by_token(token):
            return 403, "receiver revoked or unknown", None
        if self.mtx_state != "running":
            return 503, "programme output is not running", None
        s = self.ctx.settings
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(f"http://127.0.0.1:{self.ports['webrtc']}/{s.mediamtx_stream_path}/whep",
                                 content=offer_sdp.encode(), headers={"Content-Type": "application/sdp"})
        except httpx.HTTPError as e:
            return 502, f"MediaMTX unreachable: {e}", None
        return r.status_code, r.text, r.headers.get("location")

    async def whep_delete(self, token: str, session: str) -> None:
        if not self.by_token(token) or not session.startswith("/"):
            return
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                await c.delete(f"http://127.0.0.1:{self.ports['webrtc']}{session}")
        except httpx.HTTPError:
            pass

    def report(self, token: str, payload: dict) -> bool:
        r = self.by_token(token)
        if not r:
            return False
        st = self.live.setdefault(r.id, {})
        prev = st.get("state")
        st.update({k: payload.get(k) for k in ("state", "width", "height", "fps", "bitrate_kbps", "jitter_ms",
                                               "packets_lost", "rtt_ms", "error", "user_agent")})
        st["last_seen"] = time.time()
        if prev != st.get("state"):
            self.ctx.cameras.history.appendleft({"ts": time.time(), "kind": "receiver", "id": r.id, "name": r.name,
                                                 "status": st.get("state")})
            log.info("receiver %s: %s", r.name, st.get("state"))
            if st.get("state") == "failed":
                st["failures"] = st.get("failures", 0) + 1
        return True

    def link(self, r: Receiver, ip: bool = False) -> str:
        return f"{self.ctx.ip_base if ip else self.ctx.public_base}/tv/{r.token}"

    # -- MediaMTX ---------------------------------------------------------------------------------------
    def mtx_config(self) -> str:
        s = self.ctx.settings
        return f"""# generated by FestCast - edits are overwritten
logLevel: info
logDestinations: [stdout]
api: yes
apiAddress: 127.0.0.1:{self.ports['api']}
# RTSP on loopback only, TCP only (no RTP/RTCP UDP ports): live.py's ffmpeg reads the programme here
rtsp: yes
rtspTransports: [tcp]
rtspAddress: 127.0.0.1:{self.ports['rtsp']}
rtmp: no
hls: no
srt: no
moq: no
webrtc: yes
webrtcAddress: :{self.ports['webrtc']}
webrtcLocalUDPAddress: :{self.ports['ice']}
webrtcLocalTCPAddress: :{self.ports['ice']}
webrtcIPsFromInterfaces: yes
webrtcAdditionalHosts: [{self.ctx.lan_ip}]
authMethod: internal
authInternalUsers:
  - user: any
    pass:
    ips: ['127.0.0.1', '::1']
    permissions:
      - action: publish
      - action: read
      - action: api
paths:
  all_others:
"""

    @staticmethod
    def _alive(pid: int) -> bool:
        import psutil
        try:
            return psutil.Process(pid).is_running() and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
        except psutil.Error:
            return False

    async def _adoptable(self, exe: str) -> int | None:
        """A MediaMTX from our own folder that answers on the ports we recorded = safe to keep using."""
        import psutil
        from ..core import ports as P
        target = str(Path(exe).resolve()).lower()
        pids = [p.pid for p in psutil.process_iter(["exe"]) if (p.info.get("exe") or "").lower() == target]
        if len(pids) != 1:
            return None
        last = P.remembered(self.ctx.settings.data_dir)
        if not all(k in last for k in ("mtx_webrtc", "mtx_api", "mtx_ice", "mtx_rtsp")):
            return None
        try:
            async with httpx.AsyncClient(timeout=2) as c:
                r = await c.get(f"http://127.0.0.1:{last['mtx_api']}/v3/paths/list")
            if r.status_code != 200:
                return None
        except httpx.HTTPError:
            return None
        self.ports = {"webrtc": last["mtx_webrtc"], "ice": last["mtx_ice"], "api": last["mtx_api"],
                      "rtsp": last["mtx_rtsp"]}
        return pids[0]

    @staticmethod
    def _kill_stale(exe: str) -> None:
        import psutil
        target = str(Path(exe).resolve()).lower()
        for p in psutil.process_iter(["exe", "name"]):
            try:
                if (p.info.get("exe") or "").lower() == target:
                    log.warning("stopping leftover MediaMTX (pid %s) from a previous run", p.pid)
                    p.terminate()
                    p.wait(3)
            except (psutil.Error, OSError):
                pass

    def choose_ports(self) -> None:
        """Pick free ports for MediaMTX (remembered between runs so nothing moves unless it must)."""
        from ..core import ports as P
        d = self.ctx.settings.data_dir
        last = P.remembered(d)
        own = {self.ctx.settings.port}
        web = P.pick([last.get("mtx_webrtc"), self.ctx.settings.mediamtx_webrtc_port], range(8890, 8990), avoid=own)
        own.add(web)
        api = P.pick([last.get("mtx_api"), 9997], range(9998, 10100), avoid=own)
        own.add(api)
        rtsp = P.pick([last.get("mtx_rtsp"), 8554], range(8555, 8650), avoid=own)
        own.add(rtsp)
        ice = None
        for cand in [last.get("mtx_ice"), 8189, *range(8190, 8290)]:
            if cand and cand not in own and P.udp_free(cand) and P.tcp_free(cand):
                ice = cand
                break
        ice = ice or P.pick([], range(0), udp=True, avoid=own)
        new = {"webrtc": web, "ice": ice, "api": api, "rtsp": rtsp}
        if new != self.ports:
            log.info("MediaMTX ports: webrtc %s, ice %s, api %s, rtsp %s", web, ice, api, rtsp)
        self.ports = new
        P.remember(d, mtx_webrtc=web, mtx_api=api, mtx_ice=ice, mtx_rtsp=rtsp)

    async def start_mtx(self) -> None:
        exe = self.ctx.settings.mediamtx
        if not exe:
            self.mtx_state = "missing"
            self.publish()
            raise ValueError("MediaMTX not found. Run scripts\\get-mediamtx.ps1 (or put mediamtx.exe in "
                             "tools\\mediamtx\\) - see docs/SETUP.md")
        if self.proc and self.proc.returncode is None:
            return
        if self.adopted_pid and await asyncio.to_thread(self._alive, self.adopted_pid):
            return
        pid = await self._adoptable(exe)
        if pid:
            self.adopted_pid = pid
            self.want_mtx = True
            self.mtx_state = "running"
            self.mtx_log.append(f"(FestCast restarted: re-attached to the running MediaMTX, pid {pid})")
            log.info("re-attached to running MediaMTX (pid %s) - TV output not interrupted", pid)
            self.ensure_watchdog()
            self.publish()
            return
        await asyncio.to_thread(self._kill_stale, exe)
        self.choose_ports()
        cfg = (self.ctx.settings.data_dir / "mediamtx.yml").resolve()
        cfg.write_text(self.mtx_config(), "utf-8")
        self.want_mtx = True
        self.mtx_state = "starting"
        self.proc = await asyncio.create_subprocess_exec(exe, str(cfg), stdout=asyncio.subprocess.PIPE,
                                                         stderr=asyncio.subprocess.STDOUT,
                                                         cwd=str(Path(exe).parent))
        asyncio.create_task(self._pump(self.proc))
        await asyncio.sleep(1.0)
        if self.proc.returncode is not None:
            self.mtx_state = "crashed"
            self.publish()
            raise ValueError("MediaMTX exited immediately: " + " | ".join(list(self.mtx_log)[-5:]))
        self.mtx_state = "running"
        log.info("MediaMTX started (pid %s)", self.proc.pid)
        self.ensure_watchdog()
        self.publish()

    async def _pump(self, proc: asyncio.subprocess.Process) -> None:
        """Drain MediaMTX's output forever. Reads raw chunks (no line-length limit): if this reader ever stopped,
        the OS pipe would fill and MediaMTX would block - the TV output would freeze hours into the show."""
        assert proc.stdout
        buf = b""
        while True:
            try:
                chunk = await proc.stdout.read(65536)
            except Exception:  # noqa: BLE001
                log.exception("mediamtx output reader")
                await asyncio.sleep(0.5)
                continue
            if not chunk:
                return
            buf += chunk
            *lines, buf = buf.split(b"\n")
            buf = buf[-16384:]
            for raw in lines:
                t = raw.decode(errors="replace").rstrip()
                if not t:
                    continue
                self.mtx_log.append(t[:500])
                if " ERR " in t and "path not found" not in t:
                    log.warning("mediamtx: %s", t[:500])

    def ensure_watchdog(self) -> None:
        if not self._watch or self._watch.done():
            self._watch = asyncio.create_task(self._watchdog())

    async def _watchdog(self) -> None:
        """Keeps MediaMTX and the OBS -> MediaMTX stream alive for the whole show."""
        while True:
            await asyncio.sleep(3)
            try:
                adopted_ok = bool(self.adopted_pid and self._alive(self.adopted_pid))
                if self.adopted_pid and not adopted_ok:
                    self.adopted_pid = None
                    self.ctx.bus.notify("error", "MediaMTX stopped unexpectedly - restarting it. Recording is "
                                        "unaffected.", "outputs")
                if (self.want_mtx or self.want_programme) and not adopted_ok and (
                        not self.proc or self.proc.returncode is not None):
                    if self.proc is not None:
                        self.mtx_state = "crashed"
                        self.ctx.bus.notify("error", "MediaMTX stopped unexpectedly - restarting it. Recording is "
                                            "unaffected.", "outputs")
                    self.proc = None
                    await self.start_mtx()
                if self.want_programme:
                    await self._heal_programme()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("output watchdog: %s", e)

    async def _heal_programme(self) -> None:
        """If the TV output should be on but OBS isn't streaming (stream dropped, MediaMTX restarted, OBS or
        FestCast restarted) or MediaMTX has had no picture for 15 s, restart the stream, with backoff."""
        obs = self.ctx.obs
        if not obs.connected or obs.state.get("streaming") is None:
            return
        now = time.time()
        healthy = obs.state.get("streaming") and self.mtx_state == "running"
        if healthy and self.mtx_stats.get("ready") is False:
            self._not_ready_since = self._not_ready_since or now
            if now - self._not_ready_since > 15:
                healthy = False
        else:
            self._not_ready_since = None if self.mtx_stats.get("ready") else self._not_ready_since
        if healthy:
            self._heal_backoff = 5.0
            return
        if now < self._next_heal:
            return
        self._next_heal = now + self._heal_backoff
        self._heal_backoff = min(self._heal_backoff * 2, 60.0)
        self.heals += 1
        log.warning("TV output not flowing - restarting OBS stream to MediaMTX (attempt %s)", self.heals)
        try:
            if obs.state.get("streaming"):
                await obs.call("StopStream")
                for _ in range(20):
                    if not obs.state.get("streaming"):
                        break
                    await asyncio.sleep(0.25)
            await self._publish_to_mtx()
            self._not_ready_since = None
            self.ctx.bus.notify("warning", "TV output was interrupted and has been restarted automatically.",
                                "outputs")
        except Exception as e:  # noqa: BLE001
            log.warning("TV output restart failed: %s", e)

    async def stop_mtx(self) -> None:
        self.want_mtx = False
        if self.adopted_pid:
            import psutil
            try:
                psutil.Process(self.adopted_pid).terminate()
            except psutil.Error:
                pass
            self.adopted_pid = None
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                self.proc.kill()
        self.proc = None
        self.mtx_state = "stopped"
        self.publish()

    async def restart_mtx(self) -> None:
        await self.stop_mtx()
        await self.start_mtx()

    async def poll_mtx(self) -> None:
        if self.mtx_state != "running":
            self.mtx_stats = {}
            return
        s = self.ctx.settings
        try:
            async with httpx.AsyncClient(timeout=2) as c:
                p = (await c.get(f"http://127.0.0.1:{self.ports['api']}/v3/paths/get/{s.mediamtx_stream_path}"))
                sess = (await c.get(f"http://127.0.0.1:{self.ports['api']}/v3/webrtcsessions/list"))
            path = p.json() if p.status_code == 200 else {}
            sessions = sess.json().get("items", []) if sess.status_code == 200 else []
            t = time.time()
            prev = self.mtx_stats.get("_bytes", {})
            readers = []
            out_kbps = 0.0
            for x in sessions:
                if x.get("path") != s.mediamtx_stream_path or x.get("state") == "publish":
                    continue
                b = x.get("bytesSent") or 0
                kb = None
                if x["id"] in prev:
                    pb, pt = prev[x["id"]]
                    kb = max(0.0, (b - pb) * 8 / 1000 / max(0.5, t - pt))
                    out_kbps += kb
                readers.append({"id": x["id"], "remote": x.get("remoteAddr"), "kbps": kb, "bytes_sent": b,
                                "established": x.get("peerConnectionEstablished")})
            self.mtx_stats = {"ready": path.get("ready"), "source": (path.get("source") or {}).get("type"),
                              "readers": readers, "out_kbps": out_kbps if readers else 0.0,
                              "bytes_received": path.get("bytesReceived"),
                              "_bytes": {x["id"]: (x["bytes_sent"], t) for x in readers}}
        except (httpx.HTTPError, ValueError) as e:
            self.mtx_stats = {"error": str(e)}

    # -- programme publishing from OBS ----------------------------------------------------------------------
    async def start_programme(self) -> None:
        await self.start_mtx()
        obs = self.ctx.obs
        if obs.state.get("streaming") and not self.want_programme:
            raise ValueError("OBS is already streaming somewhere else. Stop that stream first "
                             "(OBS has one streaming output).")
        cur = await obs.call("GetStreamServiceSettings")
        if cur.get("streamServiceType") != "whip_custom" or "festcast" not in str(cur.get("streamServiceSettings")):
            self._saved_service = cur
            self.ctx.db.set("saved_stream_service", cur)
        await self._publish_to_mtx()
        self.want_programme = True
        self.ctx.db.set("tv_output_on", True)
        self.ensure_watchdog()
        log.info("programme publishing to MediaMTX via WHIP")
        self.ctx.bus.notify("info", "Programme output to TVs started (OBS → MediaMTX)", "outputs")
        self.publish()

    async def _publish_to_mtx(self) -> None:
        obs = self.ctx.obs
        s = self.ctx.settings
        await self.start_mtx()
        await obs.call("SetStreamServiceSettings", {
            "streamServiceType": "whip_custom",
            "streamServiceSettings": {"server": f"http://127.0.0.1:{self.ports['webrtc']}/{s.mediamtx_stream_path}"
                                                f"/whip", "bearer_token": ""}})
        await obs.call("StartStream")

    async def resume_if_wanted(self) -> None:
        """After FestCast or OBS restarts: bring the TV output back if it was on."""
        if self.want_programme:
            log.info("TV output was on before the restart - resuming")
            self._next_heal = 0.0
            self._heal_backoff = 5.0
            self.ensure_watchdog()

    async def stop_programme(self, restore_service: bool = True) -> None:
        if self.ctx.live.on:
            raise ValueError("The live stream (YouTube, Twitch, ...) is fed from the programme output. "
                             "Stop the live stream first.")
        self.want_programme = False
        self.ctx.db.set("tv_output_on", False)
        obs = self.ctx.obs
        if obs.state.get("streaming"):
            await obs.call("StopStream")
        saved = self._saved_service or self.ctx.db.get("saved_stream_service")
        if restore_service and saved and saved.get("streamServiceType"):
            for _ in range(20):
                if not obs.state.get("streaming"):
                    break
                await asyncio.sleep(0.25)
            # OBS rejects an empty settings object (its untouched default is rtmp_common with no settings);
            # {"key": ""} is the same "nothing configured" state in a form OBS accepts.
            settings = saved.get("streamServiceSettings") or {"key": ""}
            try:
                await obs.call("SetStreamServiceSettings", {"streamServiceType": saved["streamServiceType"],
                                                            "streamServiceSettings": settings})
                log.info("OBS stream service restored to %s", saved["streamServiceType"])
            except ObsError as e:
                self.ctx.bus.notify("error", f"Could not restore OBS's previous stream settings ({e.comment}). "
                                    "Check OBS > Settings > Stream before streaming elsewhere.", "outputs")
        self.publish()

    # -- HDMI --------------------------------------------------------------------------------------------------
    async def monitors(self) -> list[dict]:
        return (await self.ctx.obs.call("GetMonitorList")).get("monitors", [])

    async def open_projector(self, monitor_index: int) -> None:
        await self.ctx.obs.call("OpenVideoMixProjector", {"videoMixType": "OBS_WEBSOCKET_VIDEO_MIX_TYPE_PROGRAM",
                                                          "monitorIndex": int(monitor_index)})
        log.info("programme projector opened on monitor %s", monitor_index)

    # -- views -------------------------------------------------------------------------------------------------
    def view(self) -> dict:
        t = time.time()
        recs = []
        for r in self.receivers():
            st = self.live.get(r.id, {})
            online = bool(st.get("last_seen") and t - st["last_seen"] < 10)
            recs.append({"id": r.id, "name": r.name, "kind": r.kind, "monitor_index": r.monitor_index,
                         "link": self.link(r) if r.kind == "webrtc" else None,
                         "ip_link": self.link(r, ip=True) if r.kind == "webrtc" else None, "online": online,
                         "state": (st.get("state") if online else ("offline" if st else "never_connected")),
                         **{k: st.get(k) for k in ("width", "height", "fps", "bitrate_kbps", "jitter_ms",
                                                   "packets_lost", "rtt_ms", "error", "last_seen", "failures")}})
        return {"receivers": recs, "mediamtx": {"state": self.mtx_state, "found": bool(self.ctx.settings.mediamtx),
                                                "ports": self.ports,
                                                "log": list(self.mtx_log)[-30:],
                                                **{k: v for k, v in self.mtx_stats.items() if not k.startswith("_")}},
                "programme_streaming": self.ctx.obs.state.get("streaming")}

    def publish(self) -> None:
        self.ctx.bus.publish("outputs", self.view())

    async def shutdown(self) -> None:
        await self.stop_mtx()
        if self._watch:
            self._watch.cancel()


_ = ObsError
