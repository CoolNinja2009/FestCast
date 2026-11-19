"""OBS connection loss and resync."""
from __future__ import annotations

from backend.services.cameras import cam_input_name, cam_scene_name

from conftest import wait_for


def test_disconnect_keeps_registry_and_resync_recreates(env):
    env.create_production(2)
    c1, c2 = env.cam_ids()
    env.obs("RemoveInput", {"inputName": cam_input_name(c1)})     # someone deleted it in OBS
    env.drop()
    wait_for(lambda: not env.ctx.obs.connected and env.ctx.obs.state["connected"] is False, msg="disconnect")
    st = env.client.get("/api/state").json()
    assert st["obs"]["connected"] is False and st["obs"]["error"]
    assert [c["id"] for c in st["cameras"]["cameras"]] == [c1, c2]
    assert env.ctx.obs.owned(cam_input_name(c1)) is not None
    assert any(n["source"] == "obs" and n["level"] == "error" for n in env.ctx.bus.notifications)
    env.wait_connects(2)
    assert env.ctx.obs.state["connected"] is True
    assert cam_input_name(c1) in env.mock.inputs
    assert cam_input_name(c1) in [i["sourceName"] for i in env.scene(cam_scene_name("Camera 1"))["items"]]
    assert sum(1 for n in env.mock.inputs if n == cam_input_name(c2)) == 1


def test_actions_while_disconnected(env):
    env.create_production(1)
    env.mock.fail_connect = True
    env.drop()
    wait_for(lambda: not env.ctx.obs.connected, msg="disconnect")
    r = env.client.post("/api/program/live", json={"scene": "FC Cam · Camera 1"})
    assert r.status_code == 502 and "not connected" in r.json()["detail"]
    # layout edits are kept and marked pending until OBS is back
    r = env.client.post("/api/layouts", json={"name": "Offline", "elements": [{"type": "color", "id": "c1"}]})
    assert r.status_code == 200 and r.json()["sync"]["state"] == "pending"
    env.mock.fail_connect = False
    env.wait_connects(2, timeout=10)
    assert "FC Layout · Offline" in env.scene_names()
    assert env.ctx.layouts.sync_state[r.json()["id"]]["state"] == "synced"
