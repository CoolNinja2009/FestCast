"""Live streaming to platforms (services/live.py). ffmpeg is replaced by a small Python script that speaks ffmpeg's
-progress protocol, and MediaMTX by a stub, so these run fast and offline. The real ffmpeg + MediaMTX path
(WHIP in -> RTSP -> RTMP out, H.264 copied, Opus -> AAC) was checked by hand with both programs."""
import sys

import pytest

from conftest import wait_for

KEY = "abcd-efgh-ijkl-mnop-qrst"

FAKE_FFMPEG = r"""
import sys, threading, time, os
mode = sys.argv[1]
threading.Thread(target=lambda: (sys.stdin.read(1), os._exit(0)), daemon=True).start()
sys.stderr.write("Opening output rtmps://a.rtmps.youtube.com/live2/%s\n" % sys.argv[2]); sys.stderr.flush()
size = 0
for i in range(10000):
    if mode == "crash" and i == 3:
        sys.stderr.write("Connection reset by peer\n"); sys.stderr.flush(); sys.exit(1)
    size += 50000
    sys.stdout.write("bitrate=3000kbits/s\ntotal_size=%d\nout_time_us=%d\nprogress=continue\n" % (size, i * 100000))
    sys.stdout.flush()
    time.sleep(0.1)
"""


@pytest.fixture
def lenv(env_factory, monkeypatch, tmp_path):
    env = env_factory(ffmpeg_path=sys.executable)
    script = tmp_path / "fake_ffmpeg.py"
    script.write_text(FAKE_FFMPEG, "utf-8")
    env.mode = "ok"
    monkeypatch.setattr(env.ctx.live, "command", lambda d: [sys.executable, str(script), env.mode, d.get("key", "")])

    async def fake_mtx():
        env.ctx.outputs.mtx_state = "running"
    monkeypatch.setattr(env.ctx.outputs, "start_mtx", fake_mtx)
    yield env
    if env.ctx.live.on:
        env.client.post("/api/live/stop")


def add_youtube(env, **kw):
    r = env.client.post("/api/live/destinations", json={"platform": "youtube", "key": KEY, **kw})
    assert r.status_code == 200, r.text
    return r.json()["destinations"][-1]


def test_destination_validation_and_masking(lenv):
    c = lenv.client
    assert c.post("/api/live/destinations", json={"platform": "youtube"}).status_code == 400          # no key
    assert c.post("/api/live/destinations", json={"platform": "custom", "server": "http://x", "key": "k"}).status_code == 400
    assert c.post("/api/live/destinations", json={"platform": "myspace", "key": "k"}).status_code == 400
    d = add_youtube(lenv)
    assert d["server"] == "rtmps://a.rtmps.youtube.com/live2" and d["name"] == "YouTube"
    assert d["has_key"] and d["key_hint"] == "…qrst" and KEY not in str(lenv.client.get("/api/live").json())
    assert KEY not in lenv.client.get("/api/state").text
    # editing without a key keeps the saved one
    assert c.patch(f"/api/live/destinations/{d['id']}", json={"name": "YT main"}).status_code == 200
    saved = lenv.ctx.live.destinations()[0]
    assert saved["key"] == KEY and saved["name"] == "YT main"
    assert lenv.ctx.live.url(saved) == f"rtmps://a.rtmps.youtube.com/live2/{KEY}"
    assert KEY in lenv.ctx._secrets()                                       # redacted from diagnostics logs
    srt = c.post("/api/live/destinations", json={"platform": "custom", "server": "srt://x:9000?streamid=s3cr3t"})
    assert srt.status_code == 200 and "s3cr3t" not in srt.text
    assert c.delete(f"/api/live/destinations/{d['id']}").status_code == 200
    assert len(lenv.ctx.live.destinations()) == 1


def test_command_copies_video_and_converts_audio(env_factory):
    env = env_factory(ffmpeg_path=sys.executable)
    env.ctx.outputs.ports["rtsp"] = 8555
    cmd = env.ctx.live.command({"server": "rtmp://live.twitch.tv/app/", "key": "live_123"})
    assert cmd[cmd.index("-i") + 1] == "rtsp://127.0.0.1:8555/festcast"
    assert cmd[cmd.index("-c:v") + 1] == "copy" and cmd[cmd.index("-c:a") + 1] == "aac"
    assert cmd[-1] == "rtmp://live.twitch.tv/app/live_123" and cmd[cmd.index("-f", cmd.index("-c:a")) + 1] == "flv"
    srt = env.ctx.live.command({"server": "srt://x:9000?streamid=a", "key": ""})
    assert srt[-1] == "srt://x:9000?streamid=a" and "mpegts" in srt
    assert "rtspAddress: 127.0.0.1:" in env.ctx.outputs.mtx_config() and "rtspTransports: [tcp]" in env.ctx.outputs.mtx_config()


def test_go_live_and_stop(lenv):
    assert lenv.client.post("/api/live/start").status_code == 400          # nothing to stream to yet
    add_youtube(lenv)
    r = lenv.client.post("/api/live/start")
    assert r.status_code == 200, r.text
    assert lenv.ctx.outputs.want_programme and lenv.mock.service["streamServiceType"] == "whip_custom"
    wait_for(lambda: lenv.ctx.live.view()["destinations"][0]["state"] == "sending", msg="sending")
    wait_for(lambda: lenv.ctx.live.view()["destinations"][0]["kbps"], msg="bitrate")
    v = lenv.ctx.live.view()
    assert v["on"] and v["sending"] == 1
    log_text = "\n".join(v["destinations"][0]["log"])
    assert "<key>" in log_text and KEY not in log_text                       # ffmpeg output is redacted
    # the TV output can't be pulled from under the live stream
    assert lenv.client.post("/api/outputs/programme/stop").status_code == 400
    assert lenv.client.post("/api/live/stop").status_code == 200
    v = lenv.ctx.live.view()
    assert not v["on"] and v["destinations"][0]["state"] == "off"
    assert not lenv.ctx.outputs.want_programme                               # going live started it, so it stops too
    assert lenv.ctx.db.get("live_on") is False


def test_stop_keeps_tv_output_that_was_already_on(lenv):
    lenv.run(lenv.ctx.outputs.start_programme)
    add_youtube(lenv)
    assert lenv.client.post("/api/live/start").status_code == 200
    assert lenv.client.post("/api/live/stop").status_code == 200
    assert lenv.ctx.outputs.want_programme


def test_dropped_platform_reconnects(lenv):
    lenv.mode = "crash"
    add_youtube(lenv)
    assert lenv.client.post("/api/live/start").status_code == 200
    wait_for(lambda: lenv.ctx.live.view()["destinations"][0]["state"] == "retrying", msg="retrying")
    d = lenv.ctx.live.view()["destinations"][0]
    assert "Connection reset" in (d["error"] or "") and d["restarts"] >= 1
    assert any(n["level"] == "warning" and "YouTube" in n["message"] for n in lenv.ctx.bus.notifications)
    lenv.mode = "ok"                                                          # the platform comes back
    wait_for(lambda: lenv.ctx.live.view()["destinations"][0]["state"] == "sending", timeout=8, msg="recovered")


def test_switching_a_destination_off_while_live(lenv):
    d = add_youtube(lenv)
    add_youtube(lenv, platform="twitch", key="live_9999999999")
    assert lenv.client.post("/api/live/start").status_code == 200
    wait_for(lambda: lenv.ctx.live.view()["sending"] == 2, msg="both sending")
    assert lenv.client.patch(f"/api/live/destinations/{d['id']}", json={"enabled": False}).status_code == 200
    v = lenv.ctx.live.view()
    assert v["destinations"][0]["state"] == "off" and v["destinations"][1]["state"] == "sending"
