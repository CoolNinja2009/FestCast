"""Hall audio inputs (mic in / line in): low-latency WASAPI capture inputs nested into every FestCast scene."""
from __future__ import annotations

from backend.services.audio import BUS_SCENE, input_name
from backend.services.cameras import cam_input_name


def _items(env, scene):
    return [i["sourceName"] for i in env.scene(scene)["items"]]


def _add(env, **kw):
    body = {"name": "Stage mic", "kind": "mic", "device_id": "{sim-mic}"} | kw
    r = env.client.post("/api/audio-inputs", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_devices_listed_without_leaving_a_probe(env):
    r = env.client.get("/api/audio-inputs/devices")
    assert r.status_code == 200, r.text
    devs = {d["id"]: d for d in r.json()}
    assert devs["{sim-line}"]["guess"] == "line" and devs["{sim-mic}"]["guess"] == "mic" and "default" in devs
    assert not any(n.startswith("FC_AUDIO_") for n in env.mock.inputs)      # probe removed again
    assert not env.ctx.obs.owned("FC_AUDIO_probe")


def test_mic_input_is_low_latency_and_on_air_in_every_scene(env):
    env.create_production(2)
    a = _add(env)
    name = input_name(a["id"])
    inp = env.mock.inputs[name]
    assert inp["kind"] == "wasapi_input_capture"
    assert inp["settings"] == {"device_id": "{sim-mic}", "use_device_timing": False}
    filters = {f["filterName"]: f for f in inp["filters"]}
    assert [f["filterName"] for f in inp["filters"]] == ["FC Gate", "FC Gain", "FC Limiter"]   # gate first, limiter last
    assert filters["FC Gate"]["filterEnabled"] and filters["FC Limiter"]["filterEnabled"]
    assert not any("noise_suppress" in f["filterKind"] for f in inp["filters"])                 # adds latency
    # the bus scene holds the input and is nested in standby + camera scenes, and in layouts made later
    assert _items(env, BUS_SCENE) == [name]
    assert BUS_SCENE in _items(env, "FC Standby")
    cam_scenes = [s for s in env.scene_names() if s.startswith("FC Cam")]
    assert cam_scenes and all(BUS_SCENE in _items(env, s) for s in cam_scenes)
    cid = env.cam_ids()[0]
    r = env.client.post("/api/layouts", json={"name": "Main", "elements": [
        {"type": "camera", "id": "c1", "camera_id": cid, "x": 0, "y": 0, "w": 1, "h": 1, "z": 0}]})
    assert r.status_code == 200, r.text
    assert _items(env, "FC Layout · Main") == [cam_input_name(cid), BUS_SCENE]
    # a re-sync of the layout keeps the bus (it is not a layout element)
    env.run(env.ctx.layouts.sync, r.json()["id"])
    assert BUS_SCENE in _items(env, "FC Layout · Main")
    # not offered as a scene to put on air
    assert BUS_SCENE not in [s["name"] for s in env.client.get("/api/scenes").json()]
    # labelled in the mixer
    mix = {m["name"]: m for m in env.client.get("/api/audio").json()}
    assert mix[name]["label"] == "Stage mic" and mix[name]["is_hall"]


def test_line_input_defaults_and_processing_changes(env):
    a = _add(env, name="Desk", kind="line", device_id="{sim-line}")
    name = input_name(a["id"])
    f = {x["filterName"]: x for x in env.mock.inputs[name]["filters"]}
    assert not f["FC Gate"]["filterEnabled"] and f["FC Limiter"]["filterEnabled"]
    r = env.client.patch(f"/api/audio-inputs/{a['id']}", json={"gain_db": 99, "gate": True, "device_id": "default"})
    assert r.status_code == 200, r.text
    assert r.json()["gain_db"] == 30.0
    inp = env.mock.inputs[name]
    f = {x["filterName"]: x for x in inp["filters"]}
    assert f["FC Gain"]["filterSettings"]["db"] == 30.0 and f["FC Gate"]["filterEnabled"]
    assert inp["settings"]["device_id"] == "default"
    assert len(inp["filters"]) == 3                                  # updated in place, never duplicated


def test_isolated_tracks(env):
    env.create_production(1)
    a = _add(env, iso_track=2)
    b = _add(env, name="Desk", kind="line", device_id="{sim-line}")
    assert env.client.post("/api/audio-inputs", json={"name": "x", "kind": "mic", "iso_track": 2}).status_code == 400
    tracks = lambda n: {k for k, v in env.mock.inputs[n]["tracks"].items() if v}  # noqa: E731
    assert tracks(input_name(a["id"])) == {"1", "2"}
    assert tracks(input_name(b["id"])) == {"1"}
    assert "2" not in tracks(cam_input_name(env.cam_ids()[0]))      # owned camera input leaves the iso track
    assert env.ctx.audio.iso_mask() == 0b11
    # a non-FestCast input on that track is reported, not changed
    env.obs("CreateInput", {"sceneName": "Scene", "inputName": "Desktop Audio", "inputKind": "wasapi_output_capture",
                            "inputSettings": {}})
    v = env.client.get("/api/audio-inputs").json()
    assert v["iso_shared"] == {"2": ["Desktop Audio"]}
    assert all(env.mock.inputs["Desktop Audio"].get("tracks", {"2": True}).values())
    # turning the iso track off puts it back to programme only
    r = env.client.patch(f"/api/audio-inputs/{a['id']}", json={"iso_track": None})
    assert r.status_code == 200 and r.json()["iso_track"] is None
    assert tracks(input_name(a["id"])) == {"1"} and env.ctx.audio.iso_mask() == 1


def test_recording_writes_iso_tracks_in_advanced_mode(env):
    env.mock.profile[("Output", "Mode")] = "Advanced"
    env.mock.profile[("AdvOut", "RecTracks")] = "1"
    _add(env, iso_track=3)
    env.run(env.ctx.recording.apply_to_obs)
    assert env.mock.profile[("AdvOut", "RecTracks")] == str(0b101)


def test_inputs_survive_obs_restart_and_removal_is_clean(env):
    a = _add(env)
    name = input_name(a["id"])
    env.drop()
    env.mock.inputs.pop(name)                       # OBS came back without it
    for s in env.mock.scenes:
        s["items"] = [i for i in s["items"] if i["sourceName"] != name]
    env.wait_connects(2)
    assert name in env.mock.inputs and name in _items(env, BUS_SCENE)
    assert env.client.delete(f"/api/audio-inputs/{a['id']}").status_code == 200
    assert name not in env.mock.inputs and not env.ctx.obs.owned(name)
    assert env.client.get("/api/audio-inputs").json()["inputs"] == []
    assert env.client.delete(f"/api/audio-inputs/{a['id']}").status_code == 404
