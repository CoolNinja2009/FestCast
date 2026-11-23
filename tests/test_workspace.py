"""Production files (.festcast): save, open, new."""
from __future__ import annotations

import io
import zipfile

from PIL import Image


def _png() -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (32, 18), "red").save(b, "PNG")
    return b.getvalue()


def _setup(env):
    env.create_production(2, name="Spring Fest")
    c1, c2 = env.cam_ids()
    assert env.client.patch(f"/api/cameras/{c2}", json={"name": "Stage", "rotation": 90}).status_code == 200
    els = [{"type": "camera", "id": "e1", "camera_id": c2, "x": 0, "y": 0, "w": 1, "h": 1, "z": 0}]
    assert env.client.post("/api/layouts", json={"name": "Main", "elements": els}).status_code == 200
    r = env.client.post("/api/presentations", data={"name": "Sponsors"}, files=[("files", ("a.png", _png(), "image/png"))])
    assert r.status_code == 200, r.text
    env.client.post(f"/api/presentations/{r.json()['id']}/activate")
    env.client.put("/api/standby/message", json={"message": "Back soon"})
    return c1, c2


def test_save_and_open_round_trip(env):
    old = _setup(env)
    old_streams = {c.stream_id for c in env.ctx.cameras.cameras()}
    r = env.client.get("/api/production/file")
    assert r.status_code == 200 and "Spring%20Fest.festcast" in r.headers["content-disposition"]
    data = r.content
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert "festcast.json" in names and any(n.startswith("presentations/0/slide_") for n in names)
    assert b"stream_id" not in zipfile.ZipFile(io.BytesIO(data)).read("festcast.json")

    # open over a different workspace
    r = env.client.post("/api/production/new", json={"name": "Other", "cameras": 1, "keep": False})
    assert r.status_code == 200, r.text
    assert env.ctx.layouts.all() == [] and env.ctx.presentations.all() == []

    r = env.client.post("/api/production/file", files={"file": ("x.festcast", data, "application/octet-stream")})
    assert r.status_code == 200, r.text
    snap = r.json()
    assert snap["production"]["name"] == "Spring Fest"
    cams = env.ctx.cameras.cameras()
    assert [c.name for c in cams] == ["Camera 1", "Stage"] and cams[1].rotation == 90
    assert not ({c.stream_id for c in cams} & old_streams) and not (set(old) & {c.id for c in cams})
    (lay,) = env.ctx.layouts.all()
    assert lay.name == "Main" and lay.elements[0]["camera_id"] == cams[1].id
    (pres,) = env.ctx.presentations.all()
    assert pres.name == "Sponsors" and len(pres.slides) == 1
    assert env.ctx.db.get("active_presentation") == pres.id and env.ctx.db.get("standby_message") == "Back soon"
    assert env.client.get(f"/api/presentations/{pres.id}/slides/{pres.slides[0]['file']}").status_code == 200


def test_open_rejects_bad_files_and_keeps_workspace(env):
    env.create_production(1)
    for bad in (b"not a zip", _zip({"other.txt": b"x"}), _zip({"festcast.json": b'{"festcast": 99}'}),
                _zip({"festcast.json": b'{"festcast":1,"presentations":[{"folder":0,"slides":[{"file":"../x"}]}]}'})):
        r = env.client.post("/api/production/file", files={"file": ("x.festcast", bad, "application/octet-stream")})
        assert r.status_code == 409
    assert len(env.ctx.cameras.cameras()) == 1


def test_open_refused_while_production_running(env):
    env.create_production(1)
    data = env.client.get("/api/production/file").content
    assert env.client.post("/api/production/start").status_code == 200
    r = env.client.post("/api/production/file", files={"file": ("x.festcast", data, "application/octet-stream")})
    assert r.status_code == 409 and "running" in r.json()["detail"]
    r = env.client.post("/api/production/new", json={"name": "N", "cameras": 1})
    assert r.status_code == 409


def test_new_keeps_layouts_by_default(env):
    _setup(env)
    r = env.client.post("/api/production/new", json={"name": "Next", "cameras": 2})
    assert r.status_code == 200 and r.json()["production"]["name"] == "Next"
    assert len(env.ctx.layouts.all()) == 1 and len(env.ctx.presentations.all()) == 1


def _zip(files: dict[str, bytes]) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    return b.getvalue()


def test_manual_slide_control(env):
    files = [("files", (f"{n}.png", _png(), "image/png")) for n in "abc"]
    pid = env.client.post("/api/presentations", data={"name": "Deck"}, files=files).json()["id"]
    assert env.client.post("/api/presentations/control", json={"action": "next"}).status_code == 400  # none active
    env.client.post(f"/api/presentations/{pid}/activate")
    k = env.ctx.local_key
    st = env.client.get("/obs/standby/state", params={"k": k, "at": 0}).json()   # the page shows slide 1
    assert len(st["slides"]) == 3 and st["paused"] is False
    r = env.client.post("/api/presentations/control", json={"action": "pause"}).json()
    assert r == {"pos": 0, "count": 3, "paused": True}
    assert env.client.post("/api/presentations/control", json={"action": "prev"}).json()["pos"] == 2   # wraps
    st = env.client.get("/obs/standby/state", params={"k": k, "at": 2}).json()
    assert st["cmd_index"] == 2 and st["paused"] is True and st["cmd_seq"] == 1
    env.client.post("/api/presentations/control", json={"action": "next"})
    assert env.client.get("/obs/standby/state", params={"k": k}).json()["cmd_index"] == 0
    assert env.client.post("/api/presentations/control", json={"action": "goto", "index": 5}).status_code == 400
    assert env.client.post("/api/presentations/control", json={"action": "goto", "index": 1}).json()["pos"] == 1
    assert env.client.get("/api/presentations").json()["player"]["pos"] == 1
