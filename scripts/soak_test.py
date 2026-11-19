"""Long-run soak test against REAL OBS. Run while FestCast runs via start.bat (so crashes get restarted):

    .venv\\Scripts\\python scripts\\soak_test.py --minutes 540            # a full 9-hour rehearsal
    .venv\\Scripts\\python scripts\\soak_test.py --minutes 30 --kill-mtx-at 8 --kill-festcast-at 16

It sets up a test production (3 camera slots, a layout), records, runs the TV output, switches the programme
every minute and connects/disconnects dashboard WebSockets. Ground truth comes from its OWN OBS WebSocket
connection and MediaMTX's API, not from FestCast. Every 10 s it writes a CSV row with memory/handles of
FestCast, OBS (+ browser sources) and MediaMTX. At the end it restores your programme scene, stops what it
started, removes the test production/layout and deletes its own test recording (unless --keep-recording).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import shutil
import sys
import time
from pathlib import Path

import httpx
import psutil

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from backend.config import Settings  # noqa: E402
from backend.obs.client import ObsTransport  # noqa: E402

PORTS = ROOT / "data" / "ports.json"


def ports() -> dict:
    try:
        return json.loads(PORTS.read_text())
    except (OSError, ValueError):
        return {}


def base() -> str:
    return f"http://127.0.0.1:{ports().get('port', 8420)}"


def procs() -> dict[str, list[psutil.Process]]:
    out: dict[str, list[psutil.Process]] = {"festcast": [], "obs": [], "obs_browser": [], "mediamtx": []}
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            n = (p.info["name"] or "").lower()
            cmd = " ".join(p.info["cmdline"] or []).lower()
            if n == "obs64.exe":
                out["obs"].append(p)
            elif n == "obs-browser-page.exe":
                out["obs_browser"].append(p)
            elif n.startswith("mediamtx"):
                out["mediamtx"].append(p)
            elif "python" in n and "backend.main" in cmd:
                out["festcast"].append(p)
        except psutil.Error:
            pass
    # the venv's python.exe is a launcher that runs the real interpreter as a child: keep only the leaves
    pids = {p.pid for p in out["festcast"]}

    def leaf(p: psutil.Process) -> bool:
        try:
            return not any(c.pid in pids for c in p.children())
        except psutil.Error:
            return False
    out["festcast"] = [p for p in out["festcast"] if leaf(p)]
    return out


def mem(ps: list[psutil.Process]) -> tuple[float, int]:
    rss, handles = 0.0, 0
    for p in ps:
        try:
            rss += p.memory_info().rss / 1e6
            handles += p.num_handles() if hasattr(p, "num_handles") else p.num_fds()
        except psutil.Error:
            pass
    return round(rss, 1), handles


class Soak:
    def __init__(self, a: argparse.Namespace) -> None:
        self.a = a
        self.events: list[str] = []
        self.fail: list[str] = []
        self.out_dir = ROOT / "data" / "soak"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.csv_path = self.out_dir / f"soak_{time.strftime('%Y%m%d_%H%M%S')}.csv"
        self.obs: ObsTransport | None = None
        self.down_since: float | None = None
        self.max_down = 0.0
        self.rec_gaps = 0
        self.tv_bad_since: float | None = None
        self.max_tv_bad = 0.0

    def log(self, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')}  {msg}"
        print(line, flush=True)
        self.events.append(line)

    async def obs_call(self, req: str, data: dict | None = None) -> dict | None:
        for _ in range(2):
            try:
                if not self.obs or not self.obs.connected:
                    s = Settings()
                    self.obs = ObsTransport(s.obs_host, s.obs_port, s.obs_password, lambda *x: None)
                    await self.obs.connect()
                return await self.obs.call(req, data)
            except Exception as e:  # noqa: BLE001
                self.obs = None
                last = e
        self.log(f"OBS ground-truth call {req} failed: {last}")
        return None

    def api(self, method: str, path: str, **kw) -> httpx.Response | None:
        try:
            return httpx.request(method, base() + path, timeout=30, **kw)
        except httpx.HTTPError:
            return None

    async def wait_festcast(self, timeout: float = 120) -> bool:
        t0 = time.time()
        while time.time() - t0 < timeout:
            r = self.api("GET", "/api/health")
            if r is not None and r.status_code == 200 and r.json().get("obs") == "ok":
                return True
            await asyncio.sleep(1)
        return False

    # -- setup / teardown ------------------------------------------------------------------------------
    async def setup(self) -> None:
        if not await self.wait_festcast(30):
            raise SystemExit("FestCast is not running / not connected to OBS. Start it with start.bat first.")
        st = self.api("GET", "/api/state").json()
        if st["recording"]["active"] or st["outputs"]["programme_streaming"]:
            raise SystemExit("OBS is already recording or streaming - stop it first so the soak test can't touch it.")
        self.prev_scene = st["obs"]["program_scene"]
        self.prev_rec_cfg = st["recording"]["config"]
        self.created_prod = not st["cameras"]["production"]
        if self.created_prod:
            self.api("POST", "/api/production", json={"name": "Soak test", "cameras": 3})
        cams = self.api("GET", "/api/state").json()["cameras"]["cameras"]
        lay = self.api("POST", "/api/layouts", json={"name": "Soak layout", "elements": [
            {"type": "camera", "camera_id": c["id"], "x": i / 3, "y": 0.25, "w": 1 / 3, "h": 0.5} for i, c in enumerate(cams[:3])]
            + [{"type": "text", "text": "FestCast soak test", "x": .05, "y": .85, "w": .5, "h": .08, "z": 9}]}).json()
        self.layout = lay
        self.cam_scene = cams[0]["obs_scene"]
        self.rec_root = str(Path(self.a.record_root or (ROOT / "data" / "soak-recordings")).resolve())
        self.api("PUT", "/api/recording/config", json={**self.prev_rec_cfg, "root": self.rec_root, "auto_remux": False})
        r = self.api("POST", f"/api/layouts/{lay['id']}/live", json={"transition": "Cut"})
        self.log(f"layout live: {r.status_code if r is not None else 'no response'}")
        r = self.api("POST", "/api/recording/start")
        if r is None or r.status_code != 200:
            raise SystemExit(f"recording start failed: {r.text if r is not None else 'no response'}")
        self.rec_path = r.json().get("path")
        self.log(f"recording -> {self.rec_path}")
        recv = [x for x in self.api("GET", "/api/outputs").json()["receivers"] if x["kind"] == "webrtc"]
        if not recv:
            self.api("POST", "/api/outputs/receivers", json={"name": "Soak TV", "kind": "webrtc"})
        r = self.api("POST", "/api/outputs/programme/start")
        self.log(f"TV output start: {r.status_code if r is not None else 'no response'}")
        self.t0 = time.time()

    async def teardown(self) -> None:
        self.log("teardown")
        await self.wait_festcast(60)
        self.api("POST", "/api/outputs/programme/stop")
        r = self.api("POST", "/api/recording/stop")
        await asyncio.sleep(4)
        if self.prev_scene:
            self.api("POST", "/api/program/live", json={"scene": self.prev_scene, "transition": "Cut"})
        self.api("DELETE", f"/api/layouts/{self.layout['id']}")
        if self.created_prod:
            self.api("DELETE", "/api/production")
        self.api("PUT", "/api/recording/config", json=self.prev_rec_cfg)
        hist = (self.api("GET", "/api/recording/history") or httpx.Response(200, json=[])).json()
        self.history = [h for h in hist if h.get("path") and self.rec_root.replace("\\", "/").lower()
                        in h["path"].replace("\\", "/").lower()]
        if not self.a.keep_recording:
            shutil.rmtree(self.rec_root, ignore_errors=True)
        await self.obs_call("GetVersion")
        if self.obs:
            await self.obs.close()

    # -- checks ------------------------------------------------------------------------------------------
    async def sample(self, writer: csv.writer) -> None:
        now = time.time()
        rec = await self.obs_call("GetRecordStatus") or {}
        stream = await self.obs_call("GetStreamStatus") or {}
        stats = await self.obs_call("GetStats") or {}
        if not rec.get("outputActive"):
            self.rec_gaps += 1
            self.fail.append(f"{time.strftime('%H:%M:%S')} OBS NOT RECORDING")
        mtx_ready = None
        try:
            api_port = ports().get("mtx_api", 9997)
            mtx_ready = httpx.get(f"http://127.0.0.1:{api_port}/v3/paths/get/festcast", timeout=3).json().get("ready")
        except (httpx.HTTPError, ValueError):
            mtx_ready = False
        tv_ok = bool(stream.get("outputActive") and mtx_ready)
        if not tv_ok:
            self.tv_bad_since = self.tv_bad_since or now
            self.max_tv_bad = max(self.max_tv_bad, now - self.tv_bad_since)
        else:
            self.tv_bad_since = None
        h = self.api("GET", "/api/health")
        up = h is not None and h.status_code == 200
        if not up:
            self.down_since = self.down_since or now
            self.max_down = max(self.max_down, now - self.down_since)
        else:
            self.down_since = None
        health = h.json() if up else {}
        p = procs()
        fc, obs, br, mtx = mem(p["festcast"]), mem(p["obs"]), mem(p["obs_browser"]), mem(p["mediamtx"])
        writer.writerow([round(now - self.t0), int(bool(rec.get("outputActive"))), rec.get("outputBytes"),
                         int(bool(stream.get("outputActive"))), int(bool(mtx_ready)), int(up),
                         round(stats.get("activeFps", 0), 1), stats.get("renderSkippedFrames"),
                         stats.get("outputSkippedFrames"), fc[0], fc[1], obs[0], obs[1], br[0], len(p["obs_browser"]),
                         mtx[0], health.get("cameras_live"), json.dumps(health.get("loops_restarted", {})),
                         health.get("tv_output_auto_restarts")])

    async def churn(self, i: int) -> None:
        """Operator-like activity: switch programme every minute, open/close a dashboard socket."""
        if i % 6 == 0:
            scene = self.cam_scene if (i // 6) % 2 else f"FC Layout · {self.layout['name']}"
            self.api("POST", "/api/program/live", json={"scene": scene, "transition": "Cut"})
        if i % 12 == 3:
            try:
                import websockets
                async with websockets.connect(base().replace("http", "ws") + "/api/ws", open_timeout=5) as ws:
                    t_end = time.time() + 20
                    while time.time() < t_end:
                        await asyncio.wait_for(ws.recv(), 20)
            except Exception:  # noqa: BLE001
                pass

    def kill(self, what: str) -> None:
        for p in procs()[what]:
            try:
                p.kill()
                self.log(f"FAULT INJECTED: killed {what} (pid {p.pid})")
            except psutil.Error:
                pass

    async def run(self) -> int:
        await self.setup()
        end = self.t0 + self.a.minutes * 60
        killed_mtx = killed_fc = False
        with open(self.csv_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["t_s", "obs_recording", "rec_bytes", "obs_streaming", "mtx_ready", "festcast_up", "obs_fps",
                        "render_skipped", "output_skipped", "festcast_mb", "festcast_handles", "obs_mb",
                        "obs_handles", "browser_mb", "browser_procs", "mediamtx_mb", "cams_live",
                        "loops_restarted", "tv_auto_restarts"])
            i = 0
            try:
                while time.time() < end:
                    await self.sample(w)
                    f.flush()
                    mins = (time.time() - self.t0) / 60
                    if self.a.kill_mtx_at and not killed_mtx and mins >= self.a.kill_mtx_at:
                        killed_mtx = True
                        self.kill("mediamtx")
                    if self.a.kill_festcast_at and not killed_fc and mins >= self.a.kill_festcast_at:
                        killed_fc = True
                        self.kill("festcast")
                    if i % 30 == 0:
                        self.log(f"t+{mins:5.1f} min  festcast_up={self.down_since is None}  "
                                 f"tv_ok={self.tv_bad_since is None}  rec_gaps={self.rec_gaps}")
                    await self.churn(i)
                    i += 1
                    await asyncio.sleep(10)
            finally:
                await self.teardown()
        return self.report()

    def report(self) -> int:
        rows = list(csv.DictReader(open(self.csv_path)))
        hours = (float(rows[-1]["t_s"]) if rows else 0) / 3600

        def trend(col: str) -> str:
            pts = [(float(r["t_s"]), float(r[col])) for r in rows if r[col] not in ("", "None")]
            pts = pts[len(pts) // 5:]  # skip warm-up
            if len(pts) < 10 or hours <= 0:
                return "n/a"
            n = len(pts)
            mx = sum(t for t, _ in pts) / n
            my = sum(v for _, v in pts) / n
            slope = sum((t - mx) * (v - my) for t, v in pts) / max(1e-9, sum((t - mx) ** 2 for t, _ in pts))
            return f"{pts[-1][1]:.0f} now, {slope * 3600:+.1f}/h -> {pts[-1][1] + slope * 3600 * max(0, 9 - hours):.0f} at 9 h"

        b = [int(r["rec_bytes"]) for r in rows if r["rec_bytes"] not in ("", "None")]
        rate = (b[-1] - b[0]) * 8 / max(1, float(rows[-1]["t_s"]) - float(rows[0]["t_s"])) / 1e6 if len(b) > 1 else 0
        print("\n================ SOAK SUMMARY ================")
        print(f"duration                 {hours * 60:.1f} min ({len(rows)} samples)")
        print(f"OBS recording gaps       {self.rec_gaps}   (must be 0)")
        print(f"recording bitrate        {rate:.1f} Mb/s  -> 9 h needs about {rate * 9 * 3600 / 8 / 1000:.0f} GB")
        print(f"FestCast max downtime    {self.max_down:.0f} s")
        print(f"TV output max outage     {self.max_tv_bad:.0f} s")
        print(f"FestCast memory (MB)     {trend('festcast_mb')}")
        print(f"FestCast handles         {trend('festcast_handles')}")
        print(f"OBS memory (MB)          {trend('obs_mb')}")
        print(f"OBS browser memory (MB)  {trend('browser_mb')}")
        print(f"MediaMTX memory (MB)     {trend('mediamtx_mb')}")
        if getattr(self, "history", None):
            h = self.history[0]
            print(f"recording file           status={h['status']}  {((h.get('meta') or {}).get('actual') or {})}")
        print(f"CSV                      {self.csv_path}")
        ok = self.rec_gaps == 0
        print("RESULT:", "PASS" if ok else "FAIL")
        return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=30)
    ap.add_argument("--kill-mtx-at", type=float, default=0, help="minute at which to kill MediaMTX")
    ap.add_argument("--kill-festcast-at", type=float, default=0, help="minute at which to hard-kill FestCast")
    ap.add_argument("--record-root", default="")
    ap.add_argument("--keep-recording", action="store_true")
    return asyncio.run(Soak(ap.parse_args()).run())


if __name__ == "__main__":
    sys.exit(main())
