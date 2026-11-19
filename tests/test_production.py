"""Programme switching and the standby engine."""
from __future__ import annotations

from backend.services.production import STANDBY_SCENE

from conftest import LIVE_1080


def _setup(env, cams=2, **rules):
    env.create_production(cams)
    r = env.client.put("/api/standby/rules", json={"when_no_cameras": True, "grace_seconds": 0, **rules})
    assert r.status_code == 200
    assert env.client.post("/api/production/start").json()["started"] is True


def test_go_live(env):
    env.create_production(1)
    r = env.client.post("/api/program/live", json={"scene": "FC Cam · Camera 1"})
    assert r.status_code == 200 and r.json()["program_scene"] == "FC Cam · Camera 1"
    assert env.mock.program == "FC Cam · Camera 1"
    assert env.client.post("/api/program/live", json={"scene": "No Such"}).status_code == 502


def test_studio_preview_and_take(env):
    env.create_production(2)
    assert env.client.post("/api/program/take", json={}).status_code == 400   # nothing in preview
    r = env.client.post("/api/program/preview", json={"scene": "FC Cam · Camera 2"})
    assert r.status_code == 200
    v = r.json()
    assert v["studio_mode"] is True and v["preview_scene"] == "FC Cam · Camera 2"
    assert v["program_scene"] == "Scene"
    r = env.client.post("/api/program/take", json={"transition": "Cut"})
    assert r.status_code == 200 and r.json()["program_scene"] == "FC Cam · Camera 2"
    assert env.mock.transition == "Cut"


def test_manual_standby_and_return(env):
    env.create_production(1)
    env.client.post("/api/program/live", json={"scene": "FC Cam · Camera 1"})
    v = env.client.post("/api/standby/on").json()
    assert v["standby_active"] and v["program_scene"] == STANDBY_SCENE
    assert v["override"] == "forced_on" and v["return_scene"] == "FC Cam · Camera 1"
    v = env.client.post("/api/standby/off", json={}).json()
    assert not v["standby_active"] and v["program_scene"] == "FC Cam · Camera 1"


def test_return_without_target_400(env):
    assert env.client.post("/api/standby/off", json={}).status_code == 400


def test_bad_schedule_rejected(env):
    r = env.client.put("/api/standby/rules", json={"schedule": [{"start": "9am", "end": "10:00"}]})
    assert r.status_code == 400


def test_auto_standby_when_no_cameras(env):
    _setup(env)
    env.client.post("/api/program/live", json={"scene": "FC Cam · Camera 1"})
    env.run(env.ctx.production.evaluate)
    v = env.ctx.production.view()
    assert v["standby_active"] and v["standby_reason"] == "no usable camera feeds"
    assert v["return_scene"] == "FC Cam · Camera 1"


def test_no_auto_standby_while_one_camera_live(env):
    _setup(env)
    c1, c2 = env.cam_ids()
    env.report(c1, LIVE_1080)
    env.report(c2, LIVE_1080)
    env.client.post("/api/program/live", json={"scene": "FC Cam · Camera 1"})
    # camera 2 drops (probe reports, no video), camera 1 still live
    env.ctx.cameras.live[c2]["last_seen"] -= 100
    env.report(c2, {})
    assert env.ctx.cameras.live[c2]["status"] == "disconnected"
    env.run(env.ctx.production.evaluate)
    assert env.mock.program == "FC Cam · Camera 1"
    assert not env.ctx.production.view()["standby_active"]


def test_grace_period_delays_standby(env):
    _setup(env, grace_seconds=600)
    env.run(env.ctx.production.evaluate)
    assert env.mock.program != STANDBY_SCENE


def test_forced_off_prevents_auto_standby(env):
    _setup(env)
    assert env.client.post("/api/standby/override", params={"mode": "forced_off"}).status_code == 200
    env.run(env.ctx.production.evaluate)
    assert env.mock.program != STANDBY_SCENE
    assert env.client.post("/api/standby/override", params={"mode": "bogus"}).status_code == 400
