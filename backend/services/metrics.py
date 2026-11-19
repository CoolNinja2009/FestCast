"""Central metrics aggregation. One async loop samples every subsystem at a configurable interval, timestamps
each measurement, keeps rolling history, and pushes one `metrics` message over the WebSocket.
None means *unknown* (not measured / not available) - it is never replaced by 0."""
from __future__ import annotations

import asyncio
import logging
import shutil
import socket
import time
from collections import deque
from typing import TYPE_CHECKING, Any

import psutil

if TYPE_CHECKING:
    from ..core.context import Ctx

log = logging.getLogger("festcast.metrics")


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))   # no packet is sent; picks the default-route interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _avg(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 2) if xs else None


class MetricsService:
    def __init__(self, ctx: "Ctx") -> None:
        self.ctx = ctx
        self.started = time.time()
        self.latest: dict[str, Any] = {}
        self.history: deque[dict] = deque(maxlen=900)
        self._net_prev: tuple[float, Any] | None = None
        self._gpu: dict | None = None
        self._gpu_ts = 0.0
        self._nvsmi = shutil.which("nvidia-smi")
        psutil.cpu_percent(None)

    async def gpu(self) -> dict | None:
        if not self._nvsmi:
            return None
        if time.time() - self._gpu_ts < 4:
            return self._gpu
        self._gpu_ts = time.time()
        try:
            p = await asyncio.create_subprocess_exec(
                self._nvsmi, "--query-gpu=name,utilization.gpu,utilization.encoder,memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(p.communicate(), 3)
            parts = [x.strip() for x in out.decode().splitlines()[0].split(",")]

            def n(v: str) -> float | None:
                try:
                    return float(v)
                except ValueError:
                    return None
            self._gpu = {"name": parts[0], "util_pct": n(parts[1]), "encoder_pct": n(parts[2]),
                         "mem_used_mb": n(parts[3]), "mem_total_mb": n(parts[4]), "temp_c": n(parts[5]), "ts": time.time()}
        except Exception:  # noqa: BLE001
            self._gpu = None
        return self._gpu

    def system(self) -> dict:
        t = time.time()
        vm = psutil.virtual_memory()
        net = psutil.net_io_counters()
        rx = tx = None
        if self._net_prev:
            dt = t - self._net_prev[0]
            if dt > 0:
                rx = round((net.bytes_recv - self._net_prev[1].bytes_recv) * 8 / 1000 / dt, 1)
                tx = round((net.bytes_sent - self._net_prev[1].bytes_sent) * 8 / 1000 / dt, 1)
        self._net_prev = (t, net)
        return {"ts": t, "cpu_pct": psutil.cpu_percent(None), "ram_pct": vm.percent,
                "ram_used_gb": round(vm.used / 1024 ** 3, 2), "ram_total_gb": round(vm.total / 1024 ** 3, 1),
                "net_rx_kbps": rx, "net_tx_kbps": tx, "uptime_s": round(t - self.started)}

    async def obs(self) -> dict:
        o = self.ctx.obs
        d: dict[str, Any] = {"ts": time.time(), "connected": o.connected, "simulated": o.simulated,
                             "uptime_s": round(time.time() - o.state["connected_since"]) if o.state.get(
                                 "connected_since") else None}
        if o.connected:
            s = await o.try_call("GetStats")
            if s:
                d.update(cpu_pct=round(s.get("cpuUsage", 0), 1), mem_mb=round(s.get("memoryUsage", 0)),
                         fps=round(s.get("activeFps", 0), 2), render_ms=round(s.get("averageFrameRenderTime", 0), 2),
                         render_skipped=s.get("renderSkippedFrames"), render_total=s.get("renderTotalFrames"),
                         output_skipped=s.get("outputSkippedFrames"), output_total=s.get("outputTotalFrames"))
        return d

    def cameras(self) -> dict:
        cams = self.ctx.cameras.snapshot()["cameras"]
        live = [c for c in cams if c["status"] == "live"]
        heights = [c["height"] for c in live if c.get("height")]
        return {
            "ts": time.time(), "registered": len(cams), "connected": len(live),
            "disconnected": sum(1 for c in cams if c["status"] in ("disconnected", "waiting", "no_probe")),
            "avg_resolution": (f"{round(_avg([c['width'] for c in live if c.get('width')]) or 0)}x"
                               f"{round(_avg(heights) or 0)}" if heights else None),
            "in_kbps": round(sum(c["bitrate_in_kbps"] for c in live if c.get("bitrate_in_kbps") is not None), 1)
            if any(c.get("bitrate_in_kbps") is not None for c in live) else None,
            "avg_fps": _avg([c.get("fps") for c in live]),
            "max_packet_loss_pct": max((c["packet_loss_pct"] for c in live if c.get("packet_loss_pct") is not None),
                                       default=None),
            "below_1080p": sum(1 for c in live if c.get("height") and min(c["width"] or 0, c["height"]) < 1072),
            "poor": sum(1 for c in live if (c.get("jitter_ms") or 0) > 60 or (c.get("packet_loss_pct") or 0) > 3
                        or (c.get("fps") is not None and c["fps"] < 20)),
        }

    def outputs(self) -> dict:
        v = self.ctx.outputs.view()
        recs = [r for r in v["receivers"] if r["kind"] == "webrtc"]
        mtx = v["mediamtx"]
        return {"ts": time.time(), "registered": len(recs), "connected": sum(1 for r in recs if r["online"]
                                                                              and r["state"] == "playing"),
                "out_kbps": mtx.get("out_kbps"), "mediamtx": mtx.get("state"),
                "latency_rtt_ms": _avg([r.get("rtt_ms") for r in recs if r["online"]]),
                "failures": sum(r.get("failures") or 0 for r in recs), "programme_streaming": v["programme_streaming"]}

    def recording(self) -> dict:
        r = self.ctx.recording.status
        return {"ts": time.time(), "active": r.get("active"), "paused": r.get("paused"),
                "duration_ms": r.get("duration_ms"), "bytes": r.get("bytes"), "free_bytes": r.get("dest_free_bytes"),
                "estimated_remaining_s": self.ctx.recording.estimate_remaining_s()}

    async def sample(self) -> dict:
        self.ctx.cameras.tick()
        await self.ctx.recording.poll()
        await self.ctx.outputs.poll_mtx()
        sysm = self.system()
        sysm["gpu"] = await self.gpu()
        m = {"ts": time.time(), "system": sysm, "obs": await self.obs(), "cameras": self.cameras(),
             "outputs": self.outputs(), "recording": self.recording(),
             "network": {"lan_ip": self.ctx.lan_ip, "health": self.health_net()}}
        self.latest = m
        self.history.append({"ts": m["ts"], "cpu": sysm["cpu_pct"], "ram": sysm["ram_pct"],
                             "gpu": (sysm["gpu"] or {}).get("util_pct"), "enc": (sysm["gpu"] or {}).get("encoder_pct"),
                             "rx": sysm["net_rx_kbps"], "tx": sysm["net_tx_kbps"], "cam_in": m["cameras"]["in_kbps"],
                             "cams": m["cameras"]["connected"], "obs_fps": m["obs"].get("fps"),
                             "out": m["outputs"]["out_kbps"]})
        return m

    def health_net(self) -> str | None:
        c = self.latest.get("cameras") if self.latest else None
        if not c or not c.get("registered"):
            return None
        if c["connected"] == 0:
            return "bad"
        if c["poor"] or c["connected"] < c["registered"]:
            return "degraded"
        return "good"

    async def run(self) -> None:
        while True:
            t0 = time.time()
            try:
                m = await self.sample()
                self.ctx.bus.publish("metrics", m)
                self.ctx.cameras.publish()
                self.ctx.outputs.publish()
            except Exception:  # noqa: BLE001
                log.exception("metrics sample failed")
            await asyncio.sleep(max(0.5, self.ctx.settings.metrics_interval - (time.time() - t0)))
