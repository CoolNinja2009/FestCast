"""Real-OBS smoke test. Run while FestCast is running against your real OBS (start.bat):

    .venv\\Scripts\\python scripts\\obs_smoke_test.py [--record-seconds 8] [--keep]

It creates a production (if none), checks the OBS scenes/sources, builds a test layout, records a short clip to
data\\smoke-recordings and checks it with ffprobe, then removes the test layout (unless --keep).
It never switches your programme scene and never deletes anything FestCast did not create.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8420"
ROOT = Path(__file__).resolve().parent.parent
ok_all = True


def check(label: str, cond: bool, detail: str = "") -> None:
    global ok_all
    ok_all &= bool(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--record-seconds", type=int, default=8)
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()
    c = httpx.Client(base_url=BASE, timeout=30)

    print("1. OBS connection")
    st = c.get("/api/state").json()
    obs = st["obs"]
    check("connected to real OBS", obs["connected"] and not obs["simulated"], f"{obs.get('version')}")
    if not obs["connected"]:
        print("   ", obs.get("error"))
        return 1
    v = obs["video"] or {}
    W, H = v.get("base_w") or 1920, v.get("base_h") or 1080
    print(f"    canvas {W}x{H} -> output {v.get('out_w')}x{v.get('out_h')} @ {v.get('fps')} fps"
          + ("" if (W, H, v.get("fps")) == (1920, 1080, 30) else "  (production target is 1920x1080 @ 30)"))

    print("2. Production + camera sources")
    if not st["cameras"]["production"]:
        c.post("/api/production", json={"name": "Smoke test", "cameras": 3}).raise_for_status()
    cams = c.get("/api/state").json()["cameras"]["cameras"]
    scenes = {s["name"]: s["owned"] for s in c.get("/api/scenes").json()}
    check("standby scene exists", "FC Standby" in scenes)
    for cam in cams:
        check(f"scene for {cam['name']}", cam["obs_scene"] in scenes)
    audio = {x["name"]: x for x in c.get("/api/audio").json()}
    for cam in cams:
        check(f"browser source {cam['obs_input']} has audio channel", cam["obs_input"] in audio)
    time.sleep(6)
    cams = c.get("/api/state").json()["cameras"]["cameras"]
    for cam in cams:
        check(f"probe page running inside OBS for {cam['name']}", cam["status"] != "no_probe", cam["status"])

    print("3. Layout -> OBS transforms")
    lay = c.post("/api/layouts", json={"name": "Smoke test layout", "elements": [
        {"type": "camera", "camera_id": cams[0]["id"], "x": 0, "y": 0, "w": .5, "h": .5},
        {"type": "text", "text": "FestCast smoke test", "x": .05, "y": .8, "w": .4, "h": .08, "z": 2},
        {"type": "color", "color": "#e5484d", "x": .6, "y": .6, "w": .2, "h": .1, "z": 1}]}).json()
    check("layout synced", lay["sync"]["state"] == "synced", str(lay["sync"]))
    items = c.get(f"/api/layouts/{lay['id']}/obs").json()
    cam_item = next((i for i in items if i["sourceName"] == cams[0]["obs_input"]), None)
    t = (cam_item or {}).get("sceneItemTransform", {})
    check(f"camera item placed at 0,0 with {W // 2}x{H // 2} bounds (half the canvas)",
          cam_item is not None and abs(t.get("positionX", -1)) < 1 and abs(t.get("boundsWidth", 0) - W / 2) < 1
          and abs(t.get("boundsHeight", 0) - H / 2) < 1, str({k: t.get(k) for k in ("positionX", "positionY", "boundsWidth", "boundsHeight", "boundsType")}))
    check("3 items in layout scene", len(items) == 3, str([i["sourceName"] for i in items]))
    shot = c.post(f"/api/layouts/{lay['id']}/sync").json()
    check("re-sync idempotent", shot["state"] == "synced")

    print("4. Recording (OBS, short clip)")
    if st["recording"]["active"]:
        print("    OBS is already recording - skipping recording test so your recording is not touched")
    else:
        old = c.get("/api/recording").json()["config"]
        test_root = str(ROOT / "data" / "smoke-recordings")
        c.put("/api/recording/config", json={**old, "root": test_root, "auto_remux": False}).raise_for_status()
        applied = c.post("/api/recording/apply").json()
        for k, val in applied.items():
            if isinstance(val, dict):
                check(f"OBS accepted {k}={val['requested']}", str(val["actual"]) == str(val["requested"]), f"OBS reports {val['actual']}")
        print(f"    outputs reloaded: {applied.get('outputs_reloaded')}")
        r = c.post("/api/recording/start")
        check("recording start confirmed by OBS", r.status_code == 200, r.text[:200])
        if r.status_code == 200:
            time.sleep(a.record_seconds)
            mid = c.get("/api/recording").json()
            check("file growing", (mid.get("bytes") or 0) > 0, f"{mid.get('bytes')} bytes")
            c.post("/api/recording/stop").raise_for_status()
            time.sleep(4)
            h = c.get("/api/recording/history").json()[0]
            act = (h.get("meta") or {}).get("actual") or {}
            check("recording finished", h["status"] == "finished", h["status"])
            check("file is in the dated folder", bool(h["path"]) and str(Path(test_root).resolve()).lower() in str(Path(h["path"]).resolve()).lower(), h["path"])
            check("container is Matroska (MKV)", "matroska" in (act.get("container") or ""), act.get("container"))
            check(f"ffprobe: H.264 {v.get('out_w')}x{v.get('out_h')} (OBS output size)", act.get("video_codec") == "h264"
                  and act.get("width") == v.get("out_w") and act.get("height") == v.get("out_h"), str(act))
            check("metadata.json written", (Path(h["path"]).parent / "metadata.json").exists())
        c.put("/api/recording/config", json=old)  # restore the operator's settings
        c.post("/api/recording/apply")

    if not a.keep:
        c.delete(f"/api/layouts/{lay['id']}")
    print("\nRESULT:", "ALL PASSED" if ok_all else "SOME CHECKS FAILED")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
