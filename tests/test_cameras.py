"""Production + camera lifecycle, invitations and live monitoring."""
from __future__ import annotations

import types
from datetime import timedelta

from backend.database.models import Invitation, ObsOwned, RetiredStream, now
from backend.services import cameras as cam_mod
from backend.services.cameras import cam_input_name, cam_scene_name

from conftest import LIVE_720, LIVE_1080, REMOTE  # noqa: F401


def _token(env, cid):
    return env.ctx.cameras.invitation(cid).token


# -- production / OBS objects ---------------------------------------------------------------------------------
def test_create_production_creates_obs_objects(env):
    snap = env.create_production()
    cams = snap["cameras"]
    assert [c["name"] for c in cams] == ["Camera 1", "Camera 2", "Camera 3"]
    for c in cams:
        assert cam_scene_name(c["name"]) in env.scene_names()
        inp = env.mock.inputs[cam_input_name(c["id"])]
        assert inp["kind"] == "browser_source"
        assert f"/obs/cam/{c['id']}?k={env.ctx.local_key}" in inp["settings"]["url"]
        items = [i["sourceName"] for i in env.scene(cam_scene_name(c["name"]))["items"]]
        assert items == [cam_input_name(c["id"])]
        assert c["invite"]["link"].startswith("http://192.168.1.10:8000/join/")


def test_second_production_rejected(env):
    env.create_production()
    r = env.client.post("/api/production", json={"name": "again"})
    assert r.status_code == 400


def test_add_and_rename_camera(env):
    env.create_production(1)
    r = env.client.post("/api/cameras", json={"name": "Stage Left"})
    assert r.status_code == 200
    cid = r.json()["id"]
    assert "FC Cam · Stage Left" in env.scene_names()
    r = env.client.patch(f"/api/cameras/{cid}", json={"name": "Balcony"})
    assert r.status_code == 200 and r.json()["obs_scene"] == "FC Cam · Balcony"
    assert "FC Cam · Balcony" in env.scene_names()
    assert "FC Cam · Stage Left" not in env.scene_names()
    assert env.ctx.obs.owned("FC Cam · Balcony") and not env.ctx.obs.owned("FC Cam · Stage Left")


def test_bad_resolution_rejected(env):
    env.create_production(1)
    cid = env.cam_ids()[0]
    assert env.client.patch(f"/api/cameras/{cid}", json={"resolution_target": "4k"}).status_code == 400
    assert env.ctx.cameras.camera(cid).resolution_target == "1080p"


def test_disable_camera(env):
    env.create_production(1)
    cid = env.cam_ids()[0]
    r = env.client.patch(f"/api/cameras/{cid}", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    cfg = env.client.get(f"/obs/cam/{cid}/config", params={"k": env.ctx.local_key}).json()
    assert cfg["enabled"] is False
    env.report(cid, LIVE_1080)
    assert env.ctx.cameras.live[cid]["status"] == "disabled"
    assert env.ctx.cameras.usable_count() == 0


def test_remove_camera_only_touches_its_own_objects(env):
    env.create_production(2)
    c1, c2 = env.cam_ids()
    s1 = cam_scene_name("Camera 1")
    # an input the user created by hand in OBS, inside FestCast's camera scene
    env.obs("CreateInput", {"sceneName": s1, "inputName": "My Mic", "inputKind": "wasapi_input_capture"})
    stream = env.ctx.cameras.camera(c1).stream_id
    tok = _token(env, c1)
    assert env.client.delete(f"/api/cameras/{c1}").status_code == 200
    assert cam_input_name(c1) not in env.mock.inputs
    assert s1 not in env.scene_names()
    assert "My Mic" in env.mock.inputs                       # not FestCast's: kept
    assert cam_input_name(c2) in env.mock.inputs and cam_scene_name("Camera 2") in env.scene_names()
    with env.ctx.db.session() as s:
        assert s.query(ObsOwned).filter(ObsOwned.ref_id == c1).count() == 0
        assert s.get(RetiredStream, stream) is not None
    assert env.client.get(f"/api/public/join/{tok}").status_code == 404
    assert env.cam_ids() == [c2]


# -- invitations --------------------------------------------------------------------------------------------------
def test_invitation_resolves(env):
    env.create_production(1)
    cid = env.cam_ids()[0]
    r = env.client.get(f"/api/public/join/{_token(env, cid)}")
    assert r.status_code == 200
    d = r.json()
    assert d["camera"] == "Camera 1" and d["production"] == "Fest"
    stream = env.ctx.cameras.camera(cid).stream_id
    assert f"push={stream}" in d["publish_url"]
    assert r.headers["cache-control"] == "no-store"
    assert env.client.get("/api/public/join/nope").status_code == 404


def test_regenerate_invalidates_old_token(env):
    env.create_production(1)
    cid = env.cam_ids()[0]
    old = _token(env, cid)
    stream = env.ctx.cameras.camera(cid).stream_id
    assert env.client.post(f"/api/cameras/{cid}/invite").status_code == 200
    new = _token(env, cid)
    assert new != old
    assert env.client.get(f"/api/public/join/{old}").status_code == 404
    assert env.client.get(f"/api/public/join/{new}").status_code == 200
    assert env.ctx.cameras.camera(cid).stream_id == stream      # regenerate keeps the stream


def test_revoke_rotates_stream_and_kills_old_link(env):
    env.create_production(1)
    cid = env.cam_ids()[0]
    old_tok, old_stream = _token(env, cid), env.ctx.cameras.camera(cid).stream_id
    assert env.client.post(f"/api/cameras/{cid}/revoke").status_code == 200
    assert env.client.get(f"/api/public/join/{old_tok}").status_code == 404
    new = env.client.get(f"/api/public/join/{_token(env, cid)}").json()
    assert old_stream not in new["publish_url"]
    assert f"push={env.ctx.cameras.camera(cid).stream_id}" in new["publish_url"]


def test_qr_has_join_and_direct_links(env):
    env.create_production(1)
    cid = env.cam_ids()[0]
    q = env.client.get(f"/api/cameras/{cid}/qr").json()
    assert q["link"].endswith(f"/join/{_token(env, cid)}") and q["qr"].startswith("data:image/png")
    join = env.client.get(f"/api/public/join/{_token(env, cid)}").json()
    assert q["direct"] == join["publish_url"] and q["direct"].startswith("https://")
    assert q["direct_qr"].startswith("data:image/png")
    assert env.client.post(f"/api/cameras/{cid}/revoke").status_code == 200   # revoke kills the direct link too
    assert q["direct"] != env.client.get(f"/api/cameras/{cid}/qr").json()["direct"]


def test_expired_invitation_404(env):
    env.create_production(1)
    cid = env.cam_ids()[0]
    tok = _token(env, cid)
    with env.ctx.db.session() as s:
        s.get(Invitation, tok).expires_at = now() - timedelta(minutes=1)
    assert env.client.get(f"/api/public/join/{tok}").status_code == 404


# -- monitoring ------------------------------------------------------------------------------------------------------
def _status(env, cid):
    return next(c for c in env.client.get("/api/state").json()["cameras"]["cameras"] if c["id"] == cid)


def test_status_no_probe_live_disconnected(env, monkeypatch):
    env.create_production(1)
    cid = env.cam_ids()[0]
    assert _status(env, cid)["status"] == "no_probe"
    r = env.report(cid, LIVE_1080)
    assert r.status_code == 200 and r.json()["ok"] is True
    v = _status(env, cid)
    assert v["status"] == "live" and (v["width"], v["height"]) == (1920, 1080) and v["below_target"] is False
    assert v["jitter_ms"] is None                 # unknown stays unknown
    assert env.ctx.cameras.usable_count() == 1
    # probe keeps reporting, but no video for longer than NO_VIDEO_AFTER
    t0 = env.ctx.cameras.live[cid]["last_seen"]
    monkeypatch.setattr(cam_mod, "time", types.SimpleNamespace(time=lambda: t0 + cam_mod.NO_VIDEO_AFTER + 1))
    env.report(cid, {})
    assert _status(env, cid)["status"] == "disconnected"
    assert any("lost its feed" in n["message"] for n in env.ctx.bus.notifications)
    # probe silent too -> no_probe
    monkeypatch.setattr(cam_mod, "time", types.SimpleNamespace(time=lambda: t0 + 60))
    env.ctx.cameras.tick()
    assert _status(env, cid)["status"] == "no_probe"


def test_waiting_when_probe_but_no_video(env):
    env.create_production(1)
    cid = env.cam_ids()[0]
    env.report(cid, {})
    assert _status(env, cid)["status"] == "waiting"


def test_below_target(env):
    env.create_production(1)
    cid = env.cam_ids()[0]
    env.report(cid, LIVE_720)
    v = _status(env, cid)
    assert v["status"] == "live" and v["height"] == 720 and v["below_target"] is True


def test_stats_wrong_key_or_remote_forbidden(env):
    env.create_production(1)
    cid = env.cam_ids()[0]
    assert env.report(cid, LIVE_1080, key="wrong").status_code == 403
    assert env.client.post(f"/obs/cam/{cid}/stats", json={"stats": LIVE_1080}).status_code == 403
    assert env.report(cid, LIVE_1080, client=env.remote()).status_code == 403
    assert cid not in env.ctx.cameras.live or not env.ctx.cameras.live[cid].get("last_seen")


def test_unknown_camera_report(env):
    assert env.report("nope", LIVE_1080).json() == {"ok": False}


def test_rotation_and_frame_reach_the_obs_page(env):
    env.client.post("/api/production", json={"name": "P", "cameras": 1}).raise_for_status()
    cid = env.client.get("/api/state").json()["cameras"]["cameras"][0]["id"]
    assert env.client.patch(f"/api/cameras/{cid}", json={"rotation": 45}).status_code == 400
    assert env.client.patch(f"/api/cameras/{cid}", json={"frame": "zoom"}).status_code == 400
    r = env.client.patch(f"/api/cameras/{cid}", json={"rotation": 90, "frame": "fill"})
    assert r.status_code == 200 and r.json()["rotation"] == 90 and r.json()["frame"] == "fill"
    k = {"k": env.ctx.local_key}
    cfg = env.client.get(f"/obs/cam/{cid}/config", params=k).json()
    assert cfg["rotation"] == 90 and cfg["frame"] == "fill"
    rep = env.client.post(f"/obs/cam/{cid}/stats", params=k, json={"stats": None}).json()
    assert rep["rotation"] == 90 and rep["frame"] == "fill"


def test_portrait_feed_is_flagged_until_rotated(env):
    env.client.post("/api/production", json={"name": "P", "cameras": 1}).raise_for_status()
    cid = env.client.get("/api/state").json()["cameras"]["cameras"][0]["id"]
    k = {"k": env.ctx.local_key}
    env.client.post(f"/obs/cam/{cid}/stats", params=k,
                    json={"stats": {"Resolution": "1080 x 1920", "_FPS": 30, "video_bitrate_kbps": 5000}})
    cam = env.client.get("/api/state").json()["cameras"]["cameras"][0]
    assert cam["portrait"] is True and cam["below_target"] is False   # 1080x1920 is still full 1080p
    env.client.patch(f"/api/cameras/{cid}", json={"rotation": 90})
    assert env.client.get("/api/state").json()["cameras"]["cameras"][0]["portrait"] is False
