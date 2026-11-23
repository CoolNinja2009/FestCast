"""Layouts materialised as OBS scenes."""
from __future__ import annotations

import json

import pytest

from backend.services.cameras import cam_input_name
from backend.services.layouts import graphic_name, hex_to_abgr


@pytest.fixture
def lay(env):
    env.create_production(2)
    cid = env.cam_ids()[0]
    els = [{"type": "camera", "id": "cam1", "camera_id": cid, "x": 0, "y": 0, "w": 0.5, "h": 0.5, "z": 0},
           {"type": "text", "id": "txt1", "text": "Hello", "x": 0.1, "y": 0.8, "z": 1},
           {"type": "color", "id": "col1", "color": "#ff0000", "x": 0.5, "y": 0.5, "w": 0.5, "h": 0.25, "z": 2}]
    r = env.client.post("/api/layouts", json={"name": "Main", "elements": els})
    assert r.status_code == 200, r.text
    return env, r.json(), cid


def _items(env, scene):
    return {i["sourceName"]: i for i in env.scene(scene)["items"]}


def test_layout_creates_scene_with_transforms(lay):
    env, l, cid = lay
    assert l["obs_scene"] == "FC Layout · Main" and l["sync"]["state"] == "synced"
    items = _items(env, "FC Layout · Main")
    cam, txt, col = cam_input_name(cid), graphic_name("txt1"), graphic_name("col1")
    assert list(items) == [cam, txt, col]                     # z order
    tc = items[cam]["sceneItemTransform"]
    assert tc["positionX"] == 0 and tc["alignment"] == 5
    assert tc["boundsType"] == "OBS_BOUNDS_SCALE_INNER" and tc["boundsWidth"] == 960 and tc["boundsHeight"] == 540
    tt = items[txt]["sceneItemTransform"]
    assert tt["positionX"] == pytest.approx(0.1 * 1920) and tt["positionY"] == pytest.approx(0.8 * 1080)
    assert tt["boundsType"] == "OBS_BOUNDS_NONE" and tt["alignment"] == 5
    assert items[col]["sceneItemTransform"]["positionX"] == 960
    assert env.mock.inputs[txt]["kind"] == "text_gdiplus_v3"
    assert env.mock.inputs[txt]["settings"]["text"] == "Hello"
    colset = env.mock.inputs[col]["settings"]
    assert env.mock.inputs[col]["kind"] == "color_source_v3"
    assert colset["color"] == hex_to_abgr("#ff0000") and colset["width"] == 960 and colset["height"] == 270
    assert sum(1 for c, _ in env.mock.calls if c == "SetSceneItemTransform") >= 3


def test_update_removes_stale_items_and_graphics(lay):
    env, l, cid = lay
    els = [e for e in l["elements"] if e["id"] != "col1"]
    r = env.client.put(f"/api/layouts/{l['id']}", json={"elements": els})
    assert r.status_code == 200
    assert graphic_name("col1") not in _items(env, "FC Layout · Main")
    assert graphic_name("col1") not in env.mock.inputs
    assert graphic_name("txt1") in env.mock.inputs
    assert env.ctx.obs.owned(graphic_name("col1")) is None


def test_missing_camera_gives_warning(lay):
    env, l, cid = lay
    els = l["elements"] + [{"type": "camera", "camera_id": "doesnotexist", "name": "Ghost"}]
    r = env.client.put(f"/api/layouts/{l['id']}", json={"elements": els})
    assert r.json()["sync"]["state"] == "warning" and "Ghost" in r.json()["sync"]["error"]


def test_rename_layout_renames_scene(lay):
    env, l, _ = lay
    r = env.client.put(f"/api/layouts/{l['id']}", json={"name": "Finale"})
    assert r.json()["obs_scene"] == "FC Layout · Finale"
    assert "FC Layout · Finale" in env.scene_names() and "FC Layout · Main" not in env.scene_names()
    assert graphic_name("txt1") in _items(env, "FC Layout · Finale")


def test_delete_refused_while_live(lay):
    env, l, _ = lay
    assert env.client.post(f"/api/layouts/{l['id']}/live", json={}).status_code == 200
    assert env.mock.program == "FC Layout · Main"
    r = env.client.delete(f"/api/layouts/{l['id']}")
    assert r.status_code == 400 and "live" in r.json()["detail"]
    assert "FC Layout · Main" in env.scene_names()
    env.client.post("/api/program/live", json={"scene": "Scene"})
    assert env.client.delete(f"/api/layouts/{l['id']}").status_code == 200
    assert "FC Layout · Main" not in env.scene_names()
    assert graphic_name("txt1") not in env.mock.inputs and graphic_name("col1") not in env.mock.inputs


def test_export_import_roundtrip(lay):
    env, l, _ = lay
    r = env.client.get(f"/api/layouts/{l['id']}/export")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    data = r.json()
    assert data["festcast_layout"] == 1 and data["name"] == "Main"
    r = env.client.post("/api/layouts/import", files={"file": ("l.json", json.dumps(data).encode(), "application/json")})
    assert r.status_code == 200, r.text
    imp = r.json()
    assert imp["name"] == "Main 2" and imp["id"] != l["id"]
    assert [e["type"] for e in imp["elements"]] == [e["type"] for e in l["elements"]]
    assert {e["id"] for e in imp["elements"]}.isdisjoint({e["id"] for e in l["elements"]})
    assert "FC Layout · Main 2" in env.scene_names()
    bad = env.client.post("/api/layouts/import", files={"file": ("x.json", b'{"foo": 1}', "application/json")})
    assert bad.status_code == 400


def test_camera_removed_from_layouts(lay):
    env, l, cid = lay
    assert env.client.delete(f"/api/cameras/{cid}").status_code == 200
    els = env.ctx.layouts.get(l["id"]).elements
    assert all(e.get("camera_id") != cid for e in els) and len(els) == 2
    assert cam_input_name(cid) not in _items(env, "FC Layout · Main")



def test_camera_source_takes_picture_shape(env):
    """An upright phone gives a tall OBS source; the tile just fits it (no crop). Turning back restores 16:9."""
    env.create_production(2)
    a, b = env.cam_ids()
    els = [{"type": "camera", "id": "c1", "camera_id": a, "x": 0, "y": 0, "w": 0.5, "h": 1, "z": 0,
            "crop": {"l": 0.35, "t": 0.0259, "r": 0.35, "b": 0.0259}},       # old Landscape/Portrait crop
           {"type": "camera", "id": "c2", "camera_id": b, "x": 0.5, "y": 0, "w": 0.5, "h": 1, "z": 1}]
    l = env.client.post("/api/layouts", json={"name": "Two", "elements": els}).json()
    assert l["elements"][0]["crop"] == {"l": 0, "t": 0, "r": 0, "b": 0}       # cleaned on save
    rep = env.report(a, {"x": {"Resolution": "1080 x 1920", "_FPS": 30, "video_bitrate_kbps": 5000}}).json()
    assert (rep["width"], rep["height"]) == (1080, 1920)
    cam = env.ctx.cameras.camera(a)
    assert env.ctx.cameras.source_size(cam) == (1080, 1920)
    env.run(env.ctx.cameras.sync_camera_obs, a)
    st = env.mock.inputs[cam_input_name(a)]["settings"]
    assert (st["width"], st["height"]) == (1080, 1920)
    env.run(env.ctx.layouts.sync, l["id"])
    t = _items(env, "FC Layout · Two")[cam_input_name(a)]["sceneItemTransform"]
    assert t["cropLeft"] == t["cropRight"] == t["cropTop"] == 0 and t["boundsHeight"] == 1080
    env.client.patch(f"/api/cameras/{a}", json={"rotation": 90})              # lying on its side: wide again
    assert env.ctx.cameras.source_size(env.ctx.cameras.camera(a)) == (1920, 1080)
    env.report(a, {"x": {"Resolution": "1920 x 1080", "_FPS": 30, "video_bitrate_kbps": 5000}})
    assert env.ctx.cameras.source_size(env.ctx.cameras.camera(a)) == (1080, 1920)   # 1920x1080 turned 90



def test_auto_arrange(env):
    from backend.services.layouts import arrange
    two = arrange([16 / 9, 16 / 9], 1920, 1080)
    assert two == [(0.0, 0.25, 0.5, 0.5), (0.5, 0.25, 0.5, 0.5)]
    mixed = arrange([9 / 16, 16 / 9], 1920, 1080)
    assert mixed[0][2] < mixed[1][2] and abs(mixed[0][3] - mixed[1][3]) < 1e-9      # same height, tall one narrow
    env.create_production(2)
    a, b = env.cam_ids()
    els = [{"type": "camera", "id": "c1", "camera_id": a, "x": 0, "y": 0, "w": 0.5, "h": 1, "z": 0},
           {"type": "camera", "id": "c2", "camera_id": b, "x": 0.5, "y": 0, "w": 0.5, "h": 1, "z": 1},
           {"type": "text", "id": "t1", "text": "Hi", "x": 0.1, "y": 0.9, "z": 2}]
    lid = env.client.post("/api/layouts", json={"name": "A", "elements": els}).json()["id"]
    v = env.client.put(f"/api/layouts/{lid}", json={"auto_arrange": True}).json()
    assert v["auto_arrange"] is True
    c1 = next(e for e in v["elements"] if e["id"] == "c1")
    assert (c1["y"], c1["h"]) == (0.25, 0.5)                          # both 16:9 (unknown): side by side
    env.report(a, {"x": {"Resolution": "1080 x 1920", "_FPS": 30, "video_bitrate_kbps": 5000}})
    env.run(env.ctx.layouts.sync, lid)
    els2 = {e["id"]: e for e in env.ctx.layouts.get(lid).elements}
    assert els2["c1"]["w"] < els2["c2"]["w"] and els2["t1"]["y"] == 0.9   # tall tile for the phone; text untouched
