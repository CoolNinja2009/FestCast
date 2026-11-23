"""Local camera transport: phone --WHIP--> FestCast --> MediaMTX --WHEP--> OBS page. MediaMTX itself is faked here
(start_mtx / HTTP forwarding); the token checks, routing and stats handling are FestCast's."""
from __future__ import annotations

from conftest import LIVE_1080, REMOTE  # noqa: F401

from backend.services.cameras import cam_input_name


def _local_env(env_factory):
    e = env_factory(camera_transport="local", https_port=8443)
    calls: list[tuple] = []

    async def fake_start():
        e.ctx.outputs.want_mtx = True
        e.ctx.outputs.mtx_state = "running"
        calls.append(("start",))

    async def fake_forward(method, url, body=None):
        calls.append((method, url, body))
        if method == "POST":
            return 201, "v=0 answer", "/" + url.rsplit("/", 2)[-2] + "/whip/session-1"
        return 200, "", None

    async def fake_kick(path):
        calls.append(("kick", path))

    e.ctx.outputs.start_mtx = fake_start
    e.ctx.cameras._forward = fake_forward
    e.ctx.cameras.kick_local = fake_kick
    return e, calls


def _tok(e, cid):
    return e.ctx.cameras.invitation(cid).token


def test_new_cameras_are_local_and_start_mediamtx(env_factory):
    e, calls = _local_env(env_factory)
    cams = e.create_production(2)["cameras"]
    assert all(c["transport"] == "local" and c["https_ok"] for c in cams)
    assert ("start",) in calls
    # the OBS input is still the FestCast probe page, which plays the camera itself
    inp = e.mock.inputs[cam_input_name(cams[0]["id"])]
    assert inp["kind"] == "browser_source" and f"/obs/cam/{cams[0]['id']}?k=" in inp["settings"]["url"]
    cfg = e.client.get(f"/obs/cam/{cams[0]['id']}/config", params={"k": e.ctx.local_key}).json()
    assert cfg["transport"] == "local" and cfg["view_url"] is None


def test_join_points_to_https_camera_page(env_factory):
    e, _ = _local_env(env_factory)
    cid = e.create_production(1)["cameras"][0]["id"]
    tok = _tok(e, cid)
    j = e.remote().get(f"/api/public/join/{tok}").json()
    assert j["transport"] == "local" and j["https_port"] == 8443
    assert j["publish_url"] == f"https://192.168.1.10:8443/cam/{tok}"
    # http visit of the camera page is sent to https (camera access needs it)
    r = e.remote().get(f"/cam/{tok}", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"].startswith("https://")
    info = e.remote().get(f"/api/public/cam/{tok}").json()
    assert info["camera"] == "Camera 1" and info["height"] == 1080 and info["bitrate_kbps"] > 0


def test_phone_whip_is_token_checked_and_forwarded(env_factory):
    e, calls = _local_env(env_factory)
    cam = e.create_production(1)["cameras"][0]
    tok = _tok(e, cam["id"])
    phone = e.remote()
    assert phone.post("/api/public/cam/nope/whip", content="v=0 offer").status_code == 403
    r = phone.post(f"/api/public/cam/{tok}/whip", content="v=0 offer")
    assert r.status_code == 201 and r.text == "v=0 answer" and r.headers["X-Session"]
    sid = e.ctx.cameras.camera(cam["id"]).stream_id
    post = [c for c in calls if c[0] == "POST"][-1]
    assert post[1].endswith(f"/cam_{sid}/whip") and post[2] == "v=0 offer"
    assert ("kick", f"cam_{sid}") in calls          # a re-joining phone replaces its stale session
    # disabled camera: refused
    e.client.patch(f"/api/cameras/{cam['id']}", json={"enabled": False})
    assert phone.post(f"/api/public/cam/{tok}/whip", content="x").status_code == 409


def test_revoke_kicks_phone_and_kills_link(env_factory):
    e, calls = _local_env(env_factory)
    cam = e.create_production(1)["cameras"][0]
    tok = _tok(e, cam["id"])
    old_sid = e.ctx.cameras.camera(cam["id"]).stream_id
    assert e.client.post(f"/api/cameras/{cam['id']}/revoke").status_code == 200
    assert ("kick", f"cam_{old_sid}") in calls
    assert e.remote().post(f"/api/public/cam/{tok}/whip", content="x").status_code == 403
    assert e.remote().get(f"/api/public/cam/{tok}").status_code == 404


def test_obs_whep_only_from_this_pc(env_factory):
    e, calls = _local_env(env_factory)
    cid = e.create_production(1)["cameras"][0]["id"]
    assert e.remote().post(f"/obs/cam/{cid}/whep", params={"k": e.ctx.local_key}, content="x").status_code == 403
    r = e.client.post(f"/obs/cam/{cid}/whep", params={"k": e.ctx.local_key}, content="v=0 offer")
    assert r.status_code == 201
    assert [c for c in calls if c[0] == "POST"][-1][1].endswith("/whep")


def test_local_stats_from_obs_page(env_factory):
    e, _ = _local_env(env_factory)
    cid = e.create_production(1)["cameras"][0]["id"]
    parsed = {"width": 1920, "height": 1080, "fps": 30, "bitrate_kbps": 5800, "jitter_ms": 4,
              "packet_loss_pct": 0, "rtt_ms": 3, "audio_kbps": None, "codec": "H264"}
    e.client.post(f"/obs/cam/{cid}/stats", params={"k": e.ctx.local_key}, json={"parsed": parsed})
    v = e.ctx.cameras.view(e.ctx.cameras.camera(cid))
    assert v["status"] == "live" and (v["width"], v["height"], v["fps"]) == (1920, 1080, 30)
    assert v["codec"] == "H264" and v["audio_kbps"] is None      # unknown stays None, never 0
    # no video -> not live
    e.client.post(f"/obs/cam/{cid}/stats", params={"k": e.ctx.local_key}, json={"parsed": None})
    assert e.ctx.cameras.live[cid]["last_seen"]


def test_switch_transport(env_factory):
    e, calls = _local_env(env_factory)
    cid = e.create_production(1)["cameras"][0]["id"]
    r = e.client.patch(f"/api/cameras/{cid}", json={"transport": "vdoninja"})
    assert r.status_code == 200 and r.json()["transport"] == "vdoninja"
    tok = _tok(e, cid)
    assert e.remote().post(f"/api/public/cam/{tok}/whip", content="x").status_code == 403
    assert "vdo.ninja" in e.remote().get(f"/api/public/join/{tok}").json()["publish_url"]
    assert e.client.patch(f"/api/cameras/{cid}", json={"transport": "carrier-pigeon"}).status_code == 400


def test_transport_change_and_reconnect_refresh_obs_page(env_factory):
    e, _ = _local_env(env_factory)
    cid = e.create_production(1)["cameras"][0]["id"]
    inp = e.mock.inputs[cam_input_name(cid)]
    e.client.patch(f"/api/cameras/{cid}", json={"transport": "vdoninja"})
    assert inp.get("pressed") == ["refreshnocache"]
    e.client.post(f"/api/cameras/{cid}/reconnect")
    assert inp["pressed"] == ["refreshnocache", "refreshnocache"]
    cfg = e.client.get(f"/obs/cam/{cid}/config", params={"k": e.ctx.local_key}).json()
    assert isinstance(cfg["page"], int)


def test_phone_sets_rotation_from_its_motion_sensor(env_factory):
    e, _ = _local_env(env_factory)
    cid = e.create_production(1)["cameras"][0]["id"]
    url = f"/api/public/cam/{_tok(e, cid)}/orientation"
    k = {"k": e.ctx.local_key}

    def frames(w, hgt):
        e.client.post(f"/obs/cam/{cid}/stats", params=k, json={"parsed": {"width": w, "height": hgt, "fps": 30,
                                                                           "bitrate_kbps": 5000}})
    assert e.client.post(url, json={"held": 45}).status_code == 400
    frames(1080, 1920)                               # Portrait Orientation Lock: tall frames
    assert e.client.post(url, json={"held": 270}).json()["rotation"] == 270
    assert e.ctx.cameras.camera(cid).rotation == 270
    e.client.post(url, json={"held": 0})             # upright: a real portrait shot
    assert e.ctx.cameras.camera(cid).rotation == 0
    frames(1920, 1080)                               # lock off: the phone sends landscape itself
    assert e.client.post(url, json={"held": 90}).json()["rotation"] == 0
    assert e.client.post("/api/public/cam/nope/orientation", json={"held": 90}).status_code == 404


def test_vdoninja_camera_gets_auto_turn_page(env_factory):
    e = env_factory(camera_transport="vdoninja", https_port=8443)
    cid = e.create_production(1)["cameras"][0]["id"]
    tok = e.ctx.cameras.invitation(cid).token
    j = e.remote().get(f"/api/public/join/{tok}").json()
    assert j["transport"] == "vdoninja" and j["publish_url"].startswith("https://vdo.ninja/")
    assert j["wrap_url"] == f"https://192.168.1.10:8443/vcam/{tok}"
    r = e.remote().get(f"/vcam/{tok}", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == j["wrap_url"]
    e.client.post(f"/obs/cam/{cid}/stats", params={"k": e.ctx.local_key},
                  json={"stats": {"Resolution": "1080 x 1920", "_FPS": 30, "video_bitrate_kbps": 5000}})
    assert e.client.post(f"/api/public/cam/{tok}/orientation", json={"held": 90}).json()["rotation"] == 90
    assert e.ctx.cameras.camera(cid).rotation == 90


def test_auto_turn_switch(env_factory):
    e, _ = _local_env(env_factory)
    cid = e.create_production(1)["cameras"][0]["id"]
    url = f"/api/public/cam/{_tok(e, cid)}/orientation"
    e.client.post(f"/obs/cam/{cid}/stats", params={"k": e.ctx.local_key},
                  json={"parsed": {"width": 1080, "height": 1920, "fps": 30, "bitrate_kbps": 5000}})
    cam = lambda: next(c for c in e.client.get("/api/state").json()["cameras"]["cameras"] if c["id"] == cid)  # noqa: E731
    assert cam()["auto_turn"] is True and cam()["held"] is None           # phone never reported yet
    assert e.client.patch(f"/api/cameras/{cid}", json={"auto_turn": False}).json()["auto_turn"] is False
    assert e.client.post(url, json={"held": 90}).json()["rotation"] == 0  # off: reported, not applied
    assert cam()["held"] == 90 and cam()["rotation"] == 0
    assert e.client.patch(f"/api/cameras/{cid}", json={"auto_turn": True}).json()["rotation"] == 90   # on: follows
