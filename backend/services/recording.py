"""Recording manager. OBS records; FestCast configures it, confirms its real state from OBS events,
watches the destination, keeps history + metadata.json, and can remux MKV -> MP4 losslessly with ffmpeg.
It never reports 'recording' without OBS confirming it and never deletes recordings."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..database.models import Recording, now
from ..obs.client import ObsError

if TYPE_CHECKING:
    from ..core.context import Ctx

log = logging.getLogger("festcast.recording")

DEFAULTS = {
    "root": str(Path.home() / "Videos" / "FestCast_Recordings"),
    "format": "mkv", "encoder": "nvenc_h264", "quality": "high", "filename": "Event_%hh%mm%ss",
    "auto_start": True, "record_during_standby": True, "warn_free_gb": 30, "auto_remux": False,
    "segment_minutes": 0, "show_hours": 9,
}
FORMATS = {"mkv", "mp4", "mov", "fragmented_mp4", "hybrid_mp4"}
# encoder ids per OBS output mode (OBS 31+ native NVENC ids; older builds used jim_nvenc). We verify read-back.
ENCODERS = {
    "nvenc_h264": {"Simple": "nvenc", "Advanced": "obs_nvenc_h264_tex"},
    "nvenc_hevc": {"Simple": "nvenc_hevc", "Advanced": "obs_nvenc_hevc_tex"},
    "x264": {"Simple": "x264", "Advanced": "obs_x264"},
}


class RecordingService:
    def __init__(self, ctx: "Ctx") -> None:
        self.ctx = ctx
        self.status: dict[str, Any] = {"active": False, "paused": False, "duration_ms": None, "bytes": None,
                                       "path": None, "dest_free_bytes": None, "dest_ok": None, "applied": None,
                                       "confirmed": False}
        self._expect_stop = False
        self._confirm = asyncio.Event()
        self._last_bytes: tuple[float, int] | None = None
        self._paused_by_standby = False
        self._remux: dict[str, dict] = {}
        self._warned: dict[str, float] = {}
        self._current: str | None = None

    # -- config ---------------------------------------------------------------------------------------
    @property
    def config(self) -> dict:
        return {**DEFAULTS, **(self.ctx.db.get("recording") or {})}

    def set_config(self, cfg: dict) -> dict:
        c = self.config
        for k in DEFAULTS:
            if k in cfg:
                c[k] = cfg[k]
        if c["format"] not in FORMATS:
            raise ValueError(f"format must be one of {sorted(FORMATS)}")
        if c["encoder"] not in ENCODERS:
            raise ValueError(f"encoder must be one of {sorted(ENCODERS)}")
        root = Path(str(c["root"])).expanduser()
        if not root.is_absolute():
            raise ValueError("Recording folder must be an absolute path, e.g. D:\\FestCast\\Recordings")
        c["root"] = str(root)
        c["filename"] = "".join(ch for ch in str(c["filename"]) if ch not in '<>:"/\\|?*')[:80] or DEFAULTS["filename"]
        self.ctx.db.set("recording", c)
        self.publish()
        return c

    def day_dir(self) -> Path:
        return Path(self.config["root"]) / datetime.now().strftime("%Y-%m-%d")

    def check_destination(self, create: bool = False) -> dict:
        root = Path(self.config["root"])
        out = {"path": str(root), "exists": root.exists(), "writable": False, "free_bytes": None, "total_bytes": None}
        try:
            if create:
                self.day_dir().mkdir(parents=True, exist_ok=True)
            probe = root if root.exists() else next((p for p in root.parents if p.exists()), None)
            if probe:
                du = shutil.disk_usage(probe)
                out.update(free_bytes=du.free, total_bytes=du.total)
            if root.exists():
                t = root / ".festcast_write_test"
                t.write_bytes(b"ok")
                t.unlink()
                out["writable"] = True
        except OSError as e:
            out["error"] = str(e)
        return out

    def drives(self) -> list[dict]:
        import psutil
        res = []
        for p in psutil.disk_partitions(all=False):
            try:
                du = shutil.disk_usage(p.mountpoint)
            except OSError:
                continue
            res.append({"mount": p.mountpoint, "fs": p.fstype, "free_bytes": du.free, "total_bytes": du.total})
        return res

    # -- OBS configuration -------------------------------------------------------------------------------
    async def apply_to_obs(self) -> dict:
        obs = self.ctx.obs
        c = self.config
        d = self.day_dir()
        await asyncio.to_thread(d.mkdir, parents=True, exist_ok=True)
        await obs.call("SetRecordDirectory", {"recordDirectory": str(d)})
        mode = (await obs.call("GetProfileParameter", {"parameterCategory": "Output", "parameterName": "Mode"})
                ).get("parameterValue") or "Simple"
        sec = "SimpleOutput" if mode == "Simple" else "AdvOut"
        modekey = "Simple" if mode == "Simple" else "Advanced"
        sets = [("Output", "FilenameFormatting", c["filename"]), (sec, "RecFormat2", c["format"])]
        if mode == "Simple":
            sets.append((sec, "RecQuality", "HQ" if c["quality"] == "high" else "Small"))
        sets.append((sec, "RecEncoder", ENCODERS[c["encoder"]][modekey]))
        if mode != "Simple" and c.get("segment_minutes"):
            sets += [(sec, "RecSplitFile", "true"), (sec, "RecSplitFileType", "Time"),
                     (sec, "RecSplitFileTime", str(int(c["segment_minutes"])))]
        # OBS caches the muxer/encoder settings in its outputs: changing them in the profile is NOT used until the
        # outputs are rebuilt (verified: a "mkv" profile still wrote MP4). Remember what changes, reload if needed.
        needs_reload = False
        for cat, name, val in sets:
            before = await obs.try_call("GetProfileParameter", {"parameterCategory": cat, "parameterName": name})
            if name in ("RecFormat2", "RecEncoder", "RecQuality") and (before or {}).get("parameterValue") != val:
                needs_reload = True
            await obs.try_call("SetProfileParameter", {"parameterCategory": cat, "parameterName": name,
                                                       "parameterValue": val})
        reloaded = False
        if needs_reload:
            reloaded = await self.reload_obs_profile()
            await obs.call("SetRecordDirectory", {"recordDirectory": str(d)})
        applied: dict[str, Any] = {"output_mode": mode, "outputs_reloaded": reloaded,
                                   "directory": (await obs.call("GetRecordDirectory")).get("recordDirectory")}
        for cat, name, val in sets:
            r = await obs.try_call("GetProfileParameter", {"parameterCategory": cat, "parameterName": name})
            applied[f"{cat}.{name}"] = {"requested": val, "actual": r.get("parameterValue") if r else None}
        self.status["applied"] = applied
        return applied

    async def reload_obs_profile(self) -> bool:
        """Make OBS rebuild its outputs so new recording settings really apply: switch to a temporary profile and
        back (OBS re-reads the current profile from disk). Only when no output is active. Each switch is
        asynchronous in OBS, so every step waits for OBS to report it. Returns True if the reload completed."""
        obs = self.ctx.obs
        if obs.state.get("recording") or obs.state.get("streaming"):
            self.ctx.bus.notify("warning", "Recording settings saved, but OBS only applies a new format/encoder "
                                "after its outputs restart. Stop recording/streaming and press 'Apply to OBS' again.",
                                "recording")
            return False
        tmp = "FestCast_reload_tmp"

        async def wait(cond, timeout: float = 8.0) -> bool:
            for _ in range(int(timeout / 0.25)):
                if cond(await obs.call("GetProfileList")):
                    return True
                await asyncio.sleep(0.25)
            return False

        cur = (await obs.call("GetProfileList"))["currentProfileName"]
        ok = False
        try:
            if tmp in (await obs.call("GetProfileList"))["profiles"]:
                await obs.call("SetCurrentProfile", {"profileName": tmp})
            else:
                await obs.call("CreateProfile", {"profileName": tmp})  # OBS switches to a new profile
            if not await wait(lambda p: p["currentProfileName"] == tmp):
                raise ObsError("SetCurrentProfile", 0, "OBS did not switch to the temporary profile")
        finally:
            await obs.call("SetCurrentProfile", {"profileName": cur})
            ok = await wait(lambda p: p["currentProfileName"] == cur)
            if ok and tmp in (await obs.call("GetProfileList"))["profiles"]:
                await obs.try_call("RemoveProfile", {"profileName": tmp})
                await wait(lambda p: tmp not in p["profiles"], 4)
        if not ok:
            self.ctx.bus.notify("error", f"OBS did not return to profile '{cur}'. Check the Profile menu in OBS.",
                                "recording")
            return False
        await obs.refresh_state()
        log.info("OBS profile '%s' reloaded so recording settings apply", cur)
        return True

    # -- control ------------------------------------------------------------------------------------------
    async def start(self) -> dict:
        obs = self.ctx.obs
        if obs.state.get("recording"):
            raise ValueError("OBS is already recording")
        dest = await asyncio.to_thread(self.check_destination, True)
        if not dest["writable"]:
            raise ValueError(f"Recording folder is not writable: {dest.get('error') or dest['path']}")
        if dest["free_bytes"] is not None and dest["free_bytes"] < 2 * 1024 ** 3:
            raise ValueError("Less than 2 GB free on the recording drive")
        await self.apply_to_obs()
        self._confirm.clear()
        p = self.ctx.cameras.production()
        with self.ctx.db.session() as s:
            row = Recording(status="starting", meta={
                "event_name": p.name if p else None, "simulated": obs.simulated,
                "production_resolution": f"{(obs.state.get('video') or {}).get('out_w')}x"
                                         f"{(obs.state.get('video') or {}).get('out_h')}",
                "frame_rate": (obs.state.get("video") or {}).get("fps"),
                "encoder_requested": self.config["encoder"], "format_requested": self.config["format"],
                "cameras": [c.name for c in self.ctx.cameras.cameras()]})
            s.add(row)
        self._current = row.id
        try:
            await obs.call("StartRecord")
        except ObsError:
            self._finish(row.id, "failed")
            self._current = None
            raise
        try:
            await asyncio.wait_for(self._confirm.wait(), 15)
        except asyncio.TimeoutError:
            self._finish(row.id, "failed")
            self._current = None
            raise ValueError("OBS did not confirm the recording started (check OBS for an error dialog)")
        log.info("recording started -> %s", self.status.get("path"))
        self.ctx.bus.notify("info", f"Recording confirmed by OBS: {self.status.get('path')}", "recording")
        return self.view()

    async def stop(self) -> dict:
        if not self.ctx.obs.state.get("recording"):
            raise ValueError("OBS is not recording")
        self._expect_stop = True
        await self.ctx.obs.call("StopRecord")
        return self.view()

    async def pause(self) -> None:
        await self.ctx.obs.call("PauseRecord")

    async def resume(self) -> None:
        await self.ctx.obs.call("ResumeRecord")

    async def pause_for_standby(self) -> None:
        try:
            await self.pause()
            self._paused_by_standby = True
        except ObsError as e:
            self.ctx.bus.notify("warning", f"Could not pause recording for standby: {e.comment}", "recording")

    async def resume_after_standby(self) -> None:
        if self._paused_by_standby:
            self._paused_by_standby = False
            await self.ctx.obs.try_call("ResumeRecord")

    # -- OBS events -----------------------------------------------------------------------------------------
    async def on_record_state(self, data: dict) -> None:
        st = data.get("outputState", "")
        if st.endswith("STARTED"):
            self.status.update(active=True, paused=False, path=data.get("outputPath"), confirmed=True)
            cur = getattr(self, "_current", None)
            if cur:
                with self.ctx.db.session() as s:
                    r = s.get(Recording, cur)
                    if r:
                        r.status, r.path = "recording", data.get("outputPath")
            else:   # started from OBS itself: adopt it so history is complete
                with self.ctx.db.session() as s:
                    r = Recording(status="recording", path=data.get("outputPath"),
                                  meta={"started_outside_festcast": True, "simulated": self.ctx.obs.simulated})
                    s.add(r)
                self._current = r.id
            self._confirm.set()
        elif st.endswith("PAUSED"):
            self.status["paused"] = True
        elif st.endswith("RESUMED"):
            self.status["paused"] = False
        elif st.endswith("STOPPED"):
            path = data.get("outputPath") or self.status.get("path")
            expected = self._expect_stop
            self._expect_stop = False
            self.status.update(active=False, paused=False, confirmed=False)
            cur = getattr(self, "_current", None)
            self._current = None
            if cur:
                await self._finalize(cur, path, "finished" if expected else "stopped_externally")
            if not expected:
                self.ctx.bus.notify("error", "Recording stopped without a stop command from FestCast "
                                    "(stopped in OBS, disk full, or an encoder error). Check OBS.", "recording")
            else:
                self.ctx.bus.notify("info", f"Recording saved: {path}", "recording")
        self.publish()

    def _finish(self, rid: str, status: str) -> None:
        with self.ctx.db.session() as s:
            r = s.get(Recording, rid)
            if r:
                r.status, r.ended_at = status, now()

    async def _finalize(self, rid: str, path: str | None, status: str) -> None:
        probe = await self.ffprobe(path) if path else None
        size = None
        if path and Path(path).exists():
            size = Path(path).stat().st_size
        with self.ctx.db.session() as s:
            r = s.get(Recording, rid)
            if not r:
                return
            r.status, r.ended_at, r.path, r.size_bytes = status, now(), path, size
            started = r.started_at if r.started_at.tzinfo else r.started_at.replace(tzinfo=timezone.utc)
            meta = dict(r.meta or {})
            meta.update(start_time=started.isoformat(), end_time=r.ended_at.isoformat(),
                        duration_s=round((r.ended_at - started).total_seconds(), 1), file_size=size,
                        recording_status=status, file=path, actual=probe)
            r.meta = meta
        log.info("recording finalized %s (%s bytes)", path, size)
        want = (meta.get("format_requested") or "").replace("hybrid_", "").replace("fragmented_", "")
        got = (probe or {}).get("container") or ""
        if probe and want and not meta.get("simulated") and not (
                (want == "mkv" and "matroska" in got) or (want in ("mp4", "mov") and "mp4" in got)):
            self.ctx.bus.notify("warning", f"Recording container is '{got}' but '{want}' was requested. Press "
                                "'Apply to OBS & verify' (with recording stopped) so OBS reloads its settings.",
                                "recording")
        if path:
            await asyncio.to_thread(self._write_metadata, Path(path).parent, meta)
        if self.config.get("auto_remux") and path and path.lower().endswith(".mkv") and status != "failed":
            asyncio.create_task(self.remux(rid))

    def _write_metadata(self, folder: Path, meta: dict) -> None:
        try:
            f = folder / "metadata.json"
            data = json.loads(f.read_text("utf-8")) if f.exists() else {"recordings": []}
            data["recordings"].append(meta)
            tmp = f.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")
            os.replace(tmp, f)
        except (OSError, ValueError) as e:
            self.ctx.bus.notify("warning", f"Could not write metadata.json: {e}", "recording")

    async def ffprobe(self, path: str) -> dict | None:
        ff = self.ctx.settings.ffmpeg
        if not ff or not Path(path).exists():
            return None
        probe = str(Path(ff).with_name("ffprobe" + Path(ff).suffix))
        if not Path(probe).exists() and not shutil.which("ffprobe"):
            return None
        try:
            p = await asyncio.create_subprocess_exec(
                probe if Path(probe).exists() else "ffprobe", "-v", "error", "-show_entries",
                "stream=codec_type,codec_name,width,height,avg_frame_rate:format=format_name,duration",
                "-of", "json", path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, _ = await asyncio.wait_for(p.communicate(), 30)
            j = json.loads(out or b"{}")
            v = next((s for s in j.get("streams", []) if s.get("codec_type") == "video"), {})
            a = next((s for s in j.get("streams", []) if s.get("codec_type") == "audio"), {})
            return {"container": j.get("format", {}).get("format_name"), "video_codec": v.get("codec_name"),
                    "width": v.get("width"), "height": v.get("height"), "fps": v.get("avg_frame_rate"),
                    "audio_codec": a.get("codec_name"), "duration": j.get("format", {}).get("duration")}
        except Exception as e:  # noqa: BLE001
            log.warning("ffprobe failed: %s", e)
            return None

    # -- remux -------------------------------------------------------------------------------------------------
    async def remux(self, rid: str) -> dict:
        with self.ctx.db.session() as s:
            r = s.get(Recording, rid)
            if not r or not r.path:
                raise ValueError("unknown recording")
            src = Path(r.path)
        ff = self.ctx.settings.ffmpeg
        if not ff:
            raise ValueError("ffmpeg not found - install it or set FESTCAST_FFMPEG_PATH")
        if not src.exists():
            raise ValueError("recording file is missing")
        dst = src.with_suffix(".mp4")
        if dst.exists():
            raise ValueError(f"{dst.name} already exists (FestCast never overwrites recordings)")
        self._remux[rid] = {"state": "running", "out": str(dst)}
        self.publish()
        # stream copy only: lossless container change
        p = await asyncio.create_subprocess_exec(ff, "-hide_banner", "-loglevel", "error", "-i", str(src), "-map", "0",
                                                 "-c", "copy", "-movflags", "+faststart", str(dst),
                                                 stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await p.communicate()
        if p.returncode == 0:
            self._remux[rid] = {"state": "done", "out": str(dst)}
            with self.ctx.db.session() as s:
                s.get(Recording, rid).remux_path = str(dst)
            self.ctx.bus.notify("info", f"Remuxed to {dst.name}", "recording")
        else:
            self._remux[rid] = {"state": "failed", "error": err.decode(errors="replace")[-400:]}
            self.ctx.bus.notify("error", f"Remux failed: {self._remux[rid]['error']}", "recording")
        self.publish()
        return self._remux[rid]

    # -- monitoring -----------------------------------------------------------------------------------------------
    async def poll(self) -> None:
        obs = self.ctx.obs
        dest = await asyncio.to_thread(self.check_destination)
        self.status["dest_free_bytes"] = dest["free_bytes"]
        self.status["dest_ok"] = dest["writable"]
        warn_b = float(self.config["warn_free_gb"]) * 1024 ** 3
        if self.status["active"] or obs.state.get("recording"):
            if not dest["exists"]:
                self._warn("dest", "error", "Recording destination is no longer available!")
            elif dest["free_bytes"] is not None and dest["free_bytes"] < warn_b:
                self._warn("disk", "warning", f"Low disk space on recording drive: "
                           f"{dest['free_bytes'] / 1024 ** 3:.1f} GB free")
        if obs.connected:
            r = await obs.try_call("GetRecordStatus")
            if r:
                self.status.update(active=r.get("outputActive", False), paused=r.get("outputPaused", False),
                                   duration_ms=r.get("outputDuration"), bytes=r.get("outputBytes"))
                self._check_capacity(dest)
                if self.status["active"] and not self.status["paused"]:
                    b, t = r.get("outputBytes") or 0, time.time()
                    if self._last_bytes and b <= self._last_bytes[1] and t - self._last_bytes[0] > 20:
                        self._warn("stall", "error", "Recording file is not growing - OBS may be failing to write")
                    if not self._last_bytes or b > self._last_bytes[1]:
                        self._last_bytes = (t, b)
                else:
                    self._last_bytes = None
        self.publish()

    def _check_capacity(self, dest: dict) -> None:
        """Warn early (and every 10 min) if, at the bitrate OBS is really writing, the drive fills before the
        planned show length ends."""
        est = self.estimate_remaining_s()
        ms = self.status.get("duration_ms") or 0
        if est is None or not self.status.get("active") or ms < 60_000:
            return
        show_s = float(self.config.get("show_hours") or 9) * 3600
        needed = max(0.0, show_s - ms / 1000)
        rate = (self.status.get("bytes") or 0) / (ms / 1000) * 8 / 1e6
        if est < needed:
            self._warn("capacity", "error" if est < 3600 else "warning",
                       f"Recording drive fills in about {est / 3600:.1f} h at {rate:.0f} Mb/s, but the show needs "
                       f"{needed / 3600:.1f} h more. Choose a drive with more space or lower the quality.", every=600)

    def _warn(self, key: str, level: str, msg: str, every: float = 60) -> None:
        if time.time() - self._warned.get(key, 0) > every:
            self._warned[key] = time.time()
            self.ctx.bus.notify(level, msg, "recording")

    async def on_obs_connect(self) -> None:
        """Recovery: reconcile history with what OBS is really doing."""
        rec = await self.ctx.obs.call("GetRecordStatus")
        with self.ctx.db.session() as s:
            open_rows = list(s.query(Recording).filter(Recording.status.in_(["recording", "starting"])))
        if rec.get("outputActive"):
            self.status.update(active=True, confirmed=True)
            if open_rows:
                self._current = open_rows[-1].id
                self.status["path"] = open_rows[-1].path
                for r in open_rows[:-1]:
                    self._finish(r.id, "interrupted")
            log.info("OBS is recording (recovered after reconnect)")
        else:
            self._current = None
            self._expect_stop = False
            self.status.update(active=False, paused=False, confirmed=False)
            for r in open_rows:
                await self._finalize(r.id, r.path, "interrupted")
            self.publish()

    # -- views ------------------------------------------------------------------------------------------------------
    def history(self, limit: int = 100) -> list[dict]:
        with self.ctx.db.session() as s:
            rows = list(s.query(Recording).order_by(Recording.started_at.desc()).limit(limit))
        return [{"id": r.id, "path": r.path, "started_at": r.started_at.isoformat(),
                 "ended_at": r.ended_at.isoformat() if r.ended_at else None, "size_bytes": r.size_bytes,
                 "status": r.status, "meta": r.meta, "remux_path": r.remux_path,
                 "remux": self._remux.get(r.id)} for r in rows]

    def files(self) -> list[dict]:
        root = Path(self.config["root"])
        out = []
        if root.exists():
            for p in sorted(root.rglob("*"), reverse=True):
                if p.is_file() and p.suffix.lower() in (".mkv", ".mp4", ".mov", ".flv", ".ts", ".json"):
                    st = p.stat()
                    out.append({"path": str(p), "rel": str(p.relative_to(root)), "size_bytes": st.st_size,
                                "modified": st.st_mtime})
                    if len(out) >= 500:
                        break
        return out

    def estimate_remaining_s(self) -> float | None:
        free = self.status.get("dest_free_bytes")
        b, ms = self.status.get("bytes"), self.status.get("duration_ms")
        if not free or not b or not ms or ms < 10_000:
            return None
        return free / (b / (ms / 1000))

    def view(self) -> dict:
        return {**self.status, "config": self.config, "estimated_remaining_s": self.estimate_remaining_s(),
                "obs_recording": self.ctx.obs.state.get("recording")}

    def publish(self) -> None:
        self.ctx.bus.publish("recording", self.view())
