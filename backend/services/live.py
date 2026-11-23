"""Live streaming to platforms: YouTube, Twitch, Facebook, Kick, or any RTMP / RTMPS / SRT server, several at once.

OBS has one streaming output and FestCast uses it for the programme feed (OBS --WHIP--> MediaMTX, see outputs.py).
Platforms are fed from MediaMTX: one ffmpeg per destination reads the programme over RTSP on loopback and pushes it
on. Video is copied exactly as OBS encoded it (FestCast never encodes video); only the audio is converted (WHIP carries
Opus, RTMP needs AAC). TVs, recording and each platform are independent: a platform dropping touches nothing else, and
its ffmpeg is restarted with backoff until the operator stops the stream.

State shown is what ffmpeg reports (bytes actually leaving this PC). Whether the platform shows it is up to the
platform: "sending" never claims more. Stream keys are secrets: never logged, masked in every view, redacted from
ffmpeg's output and diagnostics."""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections import deque
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..core.context import Ctx

log = logging.getLogger("festcast.live")

# Ingest servers as each platform documents them; the stream key goes after the last "/".
PLATFORMS: dict[str, dict[str, str]] = {
    "youtube": {"label": "YouTube", "server": "rtmps://a.rtmps.youtube.com/live2"},
    "twitch": {"label": "Twitch", "server": "rtmp://live.twitch.tv/app"},
    "facebook": {"label": "Facebook", "server": "rtmps://live-api-s.facebook.com:443/rtmp"},
    "kick": {"label": "Kick", "server": ""},       # account-specific: copy "Stream URL" from Kick's dashboard
    "custom": {"label": "Custom", "server": ""},
}
SCHEMES = ("rtmp://", "rtmps://", "srt://")
STALL_S = 20          # no bytes leaving for this long while running = stuck: restart that destination


class LiveService:
    def __init__(self, ctx: "Ctx") -> None:
        self.ctx = ctx
        self.on = bool(ctx.db.get("live_on"))
        self.started_at: float | None = ctx.db.get("live_started_at") if self.on else None
        self.rt: dict[str, dict[str, Any]] = {}          # destination id -> runtime
        self._tasks: dict[str, asyncio.Task] = {}
        self._pids_file = ctx.settings.data_dir / "live_pids.json"

    # -- destinations ---------------------------------------------------------------------------------------------
    def destinations(self) -> list[dict]:
        return list(self.ctx.db.get("live_destinations") or [])

    def _save(self, ds: list[dict]) -> None:
        self.ctx.db.set("live_destinations", ds)

    @staticmethod
    def _clean(body: dict, old: dict | None = None) -> dict:
        platform = str(body.get("platform") or (old or {}).get("platform") or "custom")
        if platform not in PLATFORMS:
            raise ValueError(f"unknown platform {platform!r}")
        server = str(body.get("server") if body.get("server") is not None else (old or {}).get("server", "")).strip()
        server = server or PLATFORMS[platform]["server"]
        if not server.lower().startswith(SCHEMES):
            raise ValueError("Server must start with rtmp://, rtmps:// or srt:// (copy it from the platform's "
                             "stream settings)")
        key = body.get("key")
        key = (old or {}).get("key", "") if key is None else str(key).strip()   # None = keep the saved key
        if not key and not server.lower().startswith("srt://"):
            raise ValueError("Paste the stream key from the platform")
        name = str(body.get("name") or (old or {}).get("name") or PLATFORMS[platform]["label"]).strip()[:60]
        enabled = bool(body.get("enabled", (old or {}).get("enabled", True)))
        return {"name": name, "platform": platform, "server": server, "key": key, "enabled": enabled}

    def add(self, body: dict) -> dict:
        d = {"id": secrets.token_hex(4), **self._clean(body)}
        self._save([*self.destinations(), d])
        log.info("live destination added: %s (%s)", d["name"], d["platform"])
        if self.on and d["enabled"]:
            self._launch(d["id"])
        self.publish()
        return d

    async def update(self, did: str, body: dict) -> None:
        ds = self.destinations()
        i = next((i for i, d in enumerate(ds) if d["id"] == did), None)
        if i is None:
            raise KeyError(did)
        ds[i] = {"id": did, **self._clean(body, ds[i])}
        self._save(ds)
        if self.on:                                   # apply now: restart (or stop) just this destination
            await self._halt(did)
            if ds[i]["enabled"]:
                self._launch(did)
        self.publish()

    async def remove(self, did: str) -> None:
        ds = self.destinations()
        if not any(d["id"] == did for d in ds):
            raise KeyError(did)
        await self._halt(did)
        self.rt.pop(did, None)
        self._save([d for d in ds if d["id"] != did])
        self.publish()

    def _dest(self, did: str) -> dict | None:
        return next((d for d in self.destinations() if d["id"] == did), None)

    @staticmethod
    def url(d: dict) -> str:
        if not d.get("key"):
            return d["server"]
        return d["server"].rstrip("/") + "/" + d["key"]

    def secrets(self) -> list[str]:
        out = []
        for d in self.destinations():
            out += [d.get("key") or "", self.url(d)]
            if "?" in d["server"]:
                out.append(d["server"].split("?", 1)[1])
        return [x for x in out if x]

    def _redact(self, text: str) -> str:
        for s in sorted(self.secrets(), key=len, reverse=True):
            text = text.replace(s, "<key>")
        return text

    # -- go live / stop -------------------------------------------------------------------------------------------
    async def start(self) -> None:
        if not self.ctx.settings.ffmpeg:
            raise ValueError("ffmpeg not found. Install it (winget install Gyan.FFmpeg) or set "
                             "FESTCAST_FFMPEG_PATH, then restart FestCast.")
        ds = [d for d in self.destinations() if d.get("enabled")]
        if not ds:
            raise ValueError("Add a destination (YouTube, Twitch, ...) and switch it on first.")
        out = self.ctx.outputs
        if not out.want_programme:            # platforms are fed from the programme output: start it
            await out.start_programme()
            self.ctx.db.set("live_started_tv", True)
        self.on = True
        self.started_at = time.time()
        self.ctx.db.set("live_on", True)
        self.ctx.db.set("live_started_at", self.started_at)
        for d in ds:
            self._launch(d["id"])
        names = ", ".join(d["name"] for d in ds)
        log.info("live stream started to %s", names)
        self.ctx.bus.notify("info", f"Live stream started: {names}", "live")
        self.publish()

    async def stop(self) -> None:
        self.on = False
        self.started_at = None
        self.ctx.db.set("live_on", False)
        await asyncio.gather(*(self._halt(did) for did in list(self._tasks)))
        if self.ctx.db.get("live_started_tv"):
            self.ctx.db.set("live_started_tv", False)
            # Going live started the programme output; stop it again unless TVs are set up to watch it.
            if not self.ctx.outputs.receivers():
                await self.ctx.outputs.stop_programme()
        log.info("live stream stopped")
        self.ctx.bus.notify("info", "Live stream stopped", "live")
        self.publish()

    async def resume_if_wanted(self) -> None:
        """After a FestCast restart: the live stream was on, so bring every destination back."""
        await asyncio.to_thread(self._kill_stale)
        if self.on and self.ctx.settings.ffmpeg:
            log.info("live stream was on before the restart - resuming")
            for d in self.destinations():
                if d.get("enabled"):
                    self._launch(d["id"])
            self.publish()

    # -- one ffmpeg per destination ---------------------------------------------------------------------------
    def _launch(self, did: str) -> None:
        t = self._tasks.get(did)
        if t and not t.done():
            return
        self.rt[did] = {"state": "starting", "kbps": None, "since": None, "error": None, "restarts": 0,
                        "log": deque(maxlen=60), "proc": None, "bytes": 0, "bytes_at": None}
        self._tasks[did] = asyncio.create_task(self._run(did), name=f"live-{did}")

    async def _halt(self, did: str) -> None:
        t = self._tasks.pop(did, None)
        if t:
            t.cancel()
            await asyncio.gather(t, return_exceptions=True)
        rt = self.rt.get(did)
        if rt:
            await self._kill(rt.get("proc"))
            rt.update(state="off", proc=None, kbps=None, since=None)
        self._write_pids()

    @staticmethod
    async def _kill(proc: asyncio.subprocess.Process | None) -> None:
        if not proc or proc.returncode is not None:
            return
        try:
            proc.stdin.write(b"q") if proc.stdin else None          # ffmpeg's own clean stop (closes the stream)
            await asyncio.wait_for(proc.wait(), 3)
        except (asyncio.TimeoutError, OSError, RuntimeError, ConnectionError):
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), 3)
            except (ProcessLookupError, asyncio.TimeoutError, OSError):
                pass

    def source_url(self) -> str:
        return f"rtsp://127.0.0.1:{self.ctx.outputs.ports['rtsp']}/{self.ctx.settings.mediamtx_stream_path}"

    def command(self, d: dict) -> list[str]:
        url = self.url(d)
        fmt = ["-f", "mpegts"] if url.lower().startswith("srt://") else ["-f", "flv", "-flvflags",
                                                                          "no_duration_filesize"]
        return [self.ctx.settings.ffmpeg or "ffmpeg", "-hide_banner", "-nostats", "-loglevel", "warning",
                "-progress", "pipe:1", "-stats_period", "1",
                "-rtsp_transport", "tcp", "-timeout", "10000000", "-i", self.source_url(),
                "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy",
                "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
                *fmt, url]

    async def _run(self, did: str) -> None:
        rt = self.rt[did]
        backoff = 2.0
        notified = False
        while self.on:
            d = self._dest(did)
            if not d or not d.get("enabled"):
                return
            if self.ctx.outputs.mtx_state != "running":
                rt["state"] = "waiting"
                self.publish()
                await asyncio.sleep(2)
                continue
            rt.update(state="connecting", kbps=None, bytes=0, bytes_at=None, spawned=time.time())
            self.publish()
            began = time.time()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *self.command(d), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE)
            except OSError as e:
                rt.update(state="failed", error=f"could not start ffmpeg: {e}")
                self.publish()
                await asyncio.sleep(30)
                continue
            rt["proc"] = proc
            self._write_pids()
            try:
                await asyncio.gather(self._progress(did, proc), self._stderr(did, proc))
                await proc.wait()
            finally:
                await self._kill(proc)
            rt["proc"] = None
            if not self.on:
                return
            ran = time.time() - began
            if rt.get("since"):
                rt["restarts"] += 1
            rt.update(state="retrying", kbps=None, since=None)
            if rt["log"] and not rt.get("error"):
                last = rt["log"][-1]
                rt["error"] = ("No programme picture from OBS yet (the TV output must be publishing)"
                               if "opening input" in last.lower() else last)
            log.warning("live %s: ffmpeg ended (code %s) - %s", d["name"], proc.returncode, rt.get("error"))
            if ran > 30:
                backoff = 2.0
                notified = False
            if not notified:
                notified = True
                self.ctx.bus.notify("warning", f"Live stream to {d['name']} dropped "
                                    f"({rt.get('error') or 'connection ended'}). Reconnecting automatically.", "live")
            self.publish()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _progress(self, did: str, proc: asyncio.subprocess.Process) -> None:
        """ffmpeg -progress: key=value blocks ending in progress=continue. Bytes growing = really sending."""
        rt = self.rt[did]
        assert proc.stdout
        block: dict[str, str] = {}
        while True:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), STALL_S)
            except asyncio.TimeoutError:
                rt["error"] = f"stalled: nothing sent for {STALL_S} s"
                await self._kill(proc)
                return
            if not line:
                return
            k, _, v = line.decode(errors="replace").strip().partition("=")
            block[k] = v
            if k != "progress":
                continue
            now = time.time()
            size = int(block.get("total_size") or 0) if (block.get("total_size") or "").isdigit() else 0
            if size > rt["bytes"]:
                if rt["bytes_at"]:
                    rt["kbps"] = round((size - rt["bytes"]) * 8 / 1000 / max(0.2, now - rt["bytes_at"]))
                if rt["state"] != "sending":
                    rt.update(state="sending", since=now, error=None)
                    log.info("live %s: sending", did)
                rt.update(bytes=size, bytes_at=now)
            elif now - (rt["bytes_at"] or rt["spawned"]) > STALL_S:
                rt["error"] = f"stalled: nothing sent for {STALL_S} s"
                await self._kill(proc)
                return
            block = {}
            self.publish()

    async def _stderr(self, did: str, proc: asyncio.subprocess.Process) -> None:
        rt = self.rt[did]
        assert proc.stderr
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            t = self._redact(line.decode(errors="replace").rstrip())
            if t:
                rt["log"].append(t[:300])

    # -- ffmpeg left over from a FestCast that was killed --------------------------------------------------------
    def _write_pids(self) -> None:
        pids = [rt["proc"].pid for rt in self.rt.values() if rt.get("proc") and rt["proc"].returncode is None]
        try:
            self._pids_file.write_text(json.dumps(pids), "utf-8")
        except OSError:
            pass

    def _kill_stale(self) -> None:
        """A hard-killed FestCast leaves its ffmpegs pushing to the platforms; a second copy would fight them for
        the same stream key. Stop ours (checked by name and our loopback RTSP source) before starting again."""
        import psutil
        try:
            pids = json.loads(self._pids_file.read_text("utf-8"))
        except (OSError, ValueError):
            return
        for pid in pids:
            try:
                p = psutil.Process(int(pid))
                if "ffmpeg" in p.name().lower() and any("rtsp://127.0.0.1:" in a for a in p.cmdline()):
                    log.warning("stopping leftover live-stream ffmpeg (pid %s) from a previous run", pid)
                    p.kill()
            except (psutil.Error, ValueError, OSError):
                pass
        self._write_pids()

    # -- views ---------------------------------------------------------------------------------------------------
    @staticmethod
    def _mask(d: dict) -> dict:
        key = d.get("key") or ""
        server = d["server"].split("?", 1)[0] + ("?…" if "?" in d["server"] else "")
        return {"id": d["id"], "name": d["name"], "platform": d["platform"], "server": server,
                "enabled": d.get("enabled", True), "has_key": bool(key),
                "key_hint": ("…" + key[-4:]) if len(key) >= 12 else ("set" if key else "")}

    def view(self) -> dict:
        now = time.time()
        ds = []
        for d in self.destinations():
            rt = self.rt.get(d["id"], {})
            ds.append({**self._mask(d), "state": rt.get("state", "off") if self.on and d.get("enabled") else "off",
                       "kbps": rt.get("kbps"), "uptime_s": round(now - rt["since"]) if rt.get("since") else None,
                       "restarts": rt.get("restarts", 0), "error": rt.get("error"),
                       "log": list(rt.get("log") or [])[-12:]})
        return {"on": self.on, "started_at": self.started_at, "ffmpeg": bool(self.ctx.settings.ffmpeg),
                "destinations": ds, "platforms": {k: v for k, v in PLATFORMS.items()},
                "sending": sum(1 for d in ds if d["state"] == "sending")}

    def publish(self) -> None:
        self.ctx.bus.publish("live", self.view())

    async def shutdown(self) -> None:
        """FestCast is closing: stop the ffmpegs cleanly (the live flag stays, so a restart resumes)."""
        for did in list(self._tasks):
            t = self._tasks.pop(did)
            t.cancel()
            await asyncio.gather(t, return_exceptions=True)
            await self._kill((self.rt.get(did) or {}).get("proc"))
        self._write_pids()
