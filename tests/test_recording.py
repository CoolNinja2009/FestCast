"""Recording: config validation, OBS-confirmed state, history, metadata, external stops and recovery."""
from __future__ import annotations

import json

import pytest

from conftest import wait_for


def _history(env):
    return env.client.get("/api/recording/history").json()


def test_config_validation(env, tmp_path):
    assert env.client.put("/api/recording/config", json={"root": "relative/dir"}).status_code == 400
    assert env.client.put("/api/recording/config", json={"format": "avi"}).status_code == 400
    assert env.client.put("/api/recording/config", json={"encoder": "magic"}).status_code == 400
    r = env.client.put("/api/recording/config", json={"root": str(tmp_path / "r2"), "format": "mp4",
                                                       "filename": 'a<b>:c"?'})
    assert r.status_code == 200
    assert r.json()["format"] == "mp4" and r.json()["filename"] == "abc"
    assert env.ctx.recording.config["root"] == str(tmp_path / "r2")


def test_start_stop_history_and_metadata(env):
    env.create_production(1)
    r = env.client.post("/api/recording/start")
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["active"] is True and v["confirmed"] is True
    path = v["path"]
    day = env.ctx.recording.day_dir()
    assert path and str(day) in path and env.mock.rec["dir"] == str(day)
    h = _history(env)
    assert len(h) == 1 and h[0]["status"] == "recording" and h[0]["meta"]["event_name"] == "Fest"
    assert env.client.post("/api/recording/start").status_code == 400     # already recording
    r = env.client.post("/api/recording/stop")
    assert r.status_code == 200
    h = _history(env)[0]
    assert h["status"] == "finished" and h["ended_at"] and h["size_bytes"] > 0
    meta = json.loads((day / "metadata.json").read_text("utf-8"))
    assert meta["recordings"][-1]["recording_status"] == "finished"
    assert meta["recordings"][-1]["file"] == path
    assert env.client.post("/api/recording/stop").status_code == 400      # not recording any more


def test_unexpected_stop(env):
    assert env.client.post("/api/recording/start").status_code == 200
    env.obs("StopRecord")                                  # stopped in OBS, not by FestCast
    assert _history(env)[0]["status"] == "stopped_externally"
    assert env.ctx.recording.status["active"] is False
    errs = [n for n in env.ctx.bus.notifications if n["level"] == "error" and n["source"] == "recording"]
    assert errs and "without a stop command" in errs[0]["message"]


def test_start_refused_when_destination_not_writable(env, tmp_path):
    blocker = tmp_path / "afile"
    blocker.write_text("x")
    env.client.put("/api/recording/config", json={"root": str(blocker / "rec")})
    r = env.client.post("/api/recording/start")
    assert r.status_code == 400 and "not writable" in r.json()["detail"]
    assert env.mock.rec["active"] is False and _history(env) == []


def test_recording_started_in_obs_is_adopted(env):
    env.obs("StartRecord")
    h = _history(env)
    assert len(h) == 1 and h[0]["status"] == "recording" and h[0]["meta"]["started_outside_festcast"]


def _crash_while_recording(env):
    assert env.client.post("/api/recording/start").status_code == 200
    rid = _history(env)[0]["id"]
    env.mock.rec["active"] = False        # OBS died: no stop event ever arrives
    env.drop()
    wait_for(lambda: not env.ctx.obs.connected, msg="disconnect")
    env.wait_connects(2)
    return rid


def test_open_row_interrupted_after_reconnect(env):
    rid = _crash_while_recording(env)
    row = next(h for h in _history(env) if h["id"] == rid)
    assert row["status"] == "interrupted" and row["ended_at"]


def test_status_reset_after_interruption(env):
    _crash_while_recording(env)
    assert env.ctx.obs.state["recording"] is False
    assert env.ctx.recording.status["active"] is False


def test_recording_after_interruption_gets_new_row(env):
    rid = _crash_while_recording(env)
    env.obs("StartRecord")
    h = _history(env)
    assert len(h) == 2
    assert next(x for x in h if x["id"] == rid)["status"] == "interrupted"


def test_format_change_reloads_obs_profile(env):
    """Real OBS 33 only applies a new recording format after its outputs are rebuilt: a changed format must
    trigger the temporary-profile reload, and the operator's own profile must end up current again."""
    mock = env.mock
    mock.profile[("SimpleOutput", "RecFormat2")] = "hybrid_mp4"
    before = mock.reloads
    applied = env.run(env.ctx.recording.apply_to_obs)
    assert applied["outputs_reloaded"] is True
    assert mock.reloads == before + 2 and mock.profile_name == "Untitled" and mock.profiles == ["Untitled"]
    again = env.run(env.ctx.recording.apply_to_obs)
    assert again["outputs_reloaded"] is False and mock.reloads == before + 2


def test_no_profile_reload_while_recording(env):
    mock = env.mock
    env.client.post("/api/recording/start").raise_for_status()
    mock.profile[("SimpleOutput", "RecFormat2")] = "hybrid_mp4"
    before = mock.reloads
    applied = env.run(env.ctx.recording.apply_to_obs)
    assert applied["outputs_reloaded"] is False and mock.reloads == before
    env.client.post("/api/recording/stop")
