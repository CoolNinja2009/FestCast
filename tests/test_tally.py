"""Tally lights for camera phones (services/tally.py): red = on programme, green = in preview, read from OBS."""
from __future__ import annotations

import threading
import time

from conftest import wait_for

from backend.services.cameras import cam_input_name, cam_scene_name
from backend.services.layouts import layout_scene


def _setup(env, n=3):
    env.create_production(n)
    cams = env.ctx.cameras.cameras()
    toks = {c.id: env.ctx.cameras.invitation(c.id).token for c in cams}
    return cams, toks


def _obs(env, req, data=None):
    return env.run(env.ctx.obs.call, req, data)


def _tally(env, tok, v=-1):
    r = env.client.get(f"/api/public/cam/{tok}/tally", params={"v": v})
    assert r.status_code == 200, r.text
    return r.json()


def _state(env, cid):
    env.run(env.ctx.tally.refresh)
    return env.ctx.tally.state(cid)


def test_program_and_preview(env):
    (a, b, c), toks = _setup(env)
    _obs(env, "SetStudioModeEnabled", {"studioModeEnabled": True})
    _obs(env, "SetCurrentProgramScene", {"sceneName": cam_scene_name(a.name)})
    _obs(env, "SetCurrentPreviewScene", {"sceneName": cam_scene_name(b.name)})
    wait_for(lambda: env.ctx.tally.state(a.id) == "program" and env.ctx.tally.state(b.id) == "preview",
             msg="tally from OBS events")
    assert _tally(env, toks[a.id])["tally"] == "program"
    assert _tally(env, toks[b.id])["tally"] == "preview"
    assert _tally(env, toks[c.id])["tally"] == "off"
    # dashboard cards carry the same tally
    cards = {x["id"]: x for x in env.client.get("/api/state").json()["cameras"]["cameras"]}
    assert cards[a.id]["tally"] == "program" and cards[c.id]["tally"] == "off"
    # without studio mode there is no preview
    _obs(env, "SetStudioModeEnabled", {"studioModeEnabled": False})
    wait_for(lambda: env.ctx.tally.state(b.id) == "off", msg="preview cleared")


def test_layout_counts_visible_cameras_only(env):
    (a, b, c), toks = _setup(env)
    r = env.client.post("/api/layouts", json={"name": "Duo", "elements": [
        {"type": "camera", "camera_id": a.id, "x": 0, "y": 0, "w": 0.5, "h": 1},
        {"type": "camera", "camera_id": b.id, "x": 0.5, "y": 0, "w": 0.5, "h": 1}]})
    assert r.status_code == 200, r.text
    _obs(env, "SetCurrentProgramScene", {"sceneName": layout_scene("Duo")})
    assert _state(env, a.id) == "program" and _state(env, b.id) == "program" and _state(env, c.id) == "off"
    # hiding a camera's item in OBS takes its tally off at once
    scene = layout_scene("Duo")
    items = _obs(env, "GetSceneItemList", {"sceneName": scene})["sceneItems"]

    def find(sources):
        out = []
        for it in sources:
            if it["sourceName"] == cam_input_name(b.id):
                out.append((scene, it["sceneItemId"]))
        return out
    hits = find(items)
    assert hits                               # cameras sit directly in the layout scene
    if hits:
        _obs(env, "SetSceneItemEnabled", {"sceneName": hits[0][0], "sceneItemId": hits[0][1],
                                          "sceneItemEnabled": False})
        wait_for(lambda: env.ctx.tally.state(b.id) == "off", msg="hidden camera off")
        assert env.ctx.tally.state(a.id) == "program"


def test_nested_scene_and_unknown_when_obs_down(env):
    (a, b, c), toks = _setup(env)
    _obs(env, "CreateScene", {"sceneName": "My PiP"})
    _obs(env, "CreateSceneItem", {"sceneName": "My PiP", "sourceName": cam_scene_name(c.name)})
    _obs(env, "SetCurrentProgramScene", {"sceneName": "My PiP"})
    assert _state(env, c.id) == "program" and _state(env, a.id) == "off"
    env.drop()                                            # OBS crashes: no light, never a guess
    wait_for(lambda: env.ctx.tally.state(c.id) == "unknown", msg="unknown while OBS is down")
    env.wait_connects(2)
    wait_for(lambda: env.ctx.tally.state(c.id) == "program", msg="back after reconnect")


def test_long_poll_waits_for_change_and_bad_token(env):
    (a, b, c), toks = _setup(env)
    assert env.client.get("/api/public/cam/nope/tally").status_code == 404
    _obs(env, "SetCurrentProgramScene", {"sceneName": cam_scene_name(b.name)})
    first = _tally(env, toks[a.id])
    wait_for(lambda: env.ctx.tally.state(b.id) == "program", msg="settled")
    first = _tally(env, toks[a.id])
    assert first["tally"] == "off"
    got: dict = {}

    def poll():
        t = time.time()
        got.update(_tally(env, toks[a.id], first["v"]), took=time.time() - t)
    th = threading.Thread(target=poll)
    th.start()
    time.sleep(0.4)
    assert not got                                       # still waiting: nothing changed
    _obs(env, "SetCurrentProgramScene", {"sceneName": cam_scene_name(a.name)})
    th.join(5)
    assert got["tally"] == "program" and got["v"] > first["v"] and got["took"] < 5
