"""Auth, OBS-page protection, diagnostics redaction, TV receivers / MediaMTX, metrics."""
from __future__ import annotations

import io
import zipfile

import pytest

from backend.database.models import Invitation, RetiredStream

from conftest import LIVE_1080


# -- auth ----------------------------------------------------------------------------------------------------------
def test_local_is_admin_remote_is_not(env):
    assert env.client.get("/api/state").status_code == 200
    rem = env.remote()
    assert rem.get("/api/state").status_code == 401
    assert rem.post("/api/production", json={}).status_code == 401
    assert rem.get("/api/auth/me").json() == {"admin": False, "local": False, "remote_enabled": False}


def test_remote_login_disabled_without_password(env):
    r = env.remote().post("/api/auth/login", json={"password": "anything"})
    assert r.status_code == 403


def test_remote_login_with_password(env_factory):
    env = env_factory(admin_password="s3cret-pass")
    rem = env.remote()
    assert rem.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
    r = rem.post("/api/auth/login", json={"password": "s3cret-pass"})
    assert r.status_code == 200 and "fc_session" in r.cookies
    assert "httponly" in r.headers["set-cookie"].lower()
    assert rem.get("/api/state").status_code == 200
    assert rem.post("/api/auth/logout").status_code == 200
    rem.cookies.clear()
    assert rem.get("/api/state").status_code == 401
    other = env.remote()
    other.cookies.set("fc_session", "forged")
    assert other.get("/api/state").status_code == 401


def test_login_rate_limit(env_factory):
    env = env_factory(admin_password="s3cret-pass")
    rem = env.remote()
    for _ in range(8):
        assert rem.post("/api/auth/login", json={"password": "nope"}).status_code == 401
    assert rem.post("/api/auth/login", json={"password": "nope"}).status_code == 429
    assert rem.post("/api/auth/login", json={"password": "s3cret-pass"}).status_code == 429
    # other hosts are not affected
    assert env.client.post("/api/auth/login", json={"password": "s3cret-pass"}).status_code == 200


def test_obs_pages_need_loopback_and_key(env):
    env.create_production(1)
    cid = env.cam_ids()[0]
    k = env.ctx.local_key
    assert env.client.get(f"/obs/cam/{cid}", params={"k": k}).status_code == 200
    assert env.client.get(f"/obs/cam/{cid}", params={"k": "bad"}).status_code == 403
    assert env.client.get(f"/obs/cam/{cid}").status_code == 403
    rem = env.remote()
    assert rem.get(f"/obs/cam/{cid}", params={"k": k}).status_code == 403
    assert rem.get(f"/obs/cam/{cid}/config", params={"k": k}).status_code == 403
    assert rem.get("/obs/standby", params={"k": k}).status_code == 403
    cfg = env.client.get(f"/obs/cam/{cid}/config", params={"k": k}).json()
    assert "view=" in cfg["view_url"] and "cleanoutput" in cfg["view_url"]


def test_security_headers(env):
    r = env.client.get("/api/state")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "SAMEORIGIN"


def test_tv_bad_token(env):
    assert env.remote().post("/api/public/tv/bad/stats", json={"state": "playing"}).status_code == 403
    assert env.remote().get("/api/public/tv/bad").status_code == 403
    assert env.remote().post("/api/public/tv/bad/whep", content=b"v=0").status_code == 403


def test_diagnostics_contains_no_secrets(env_factory):
    env = env_factory(admin_password="admin-pw-xyz", obs_password="obs-pw-xyz")
    env.create_production(2)
    c1, c2 = env.cam_ids()
    env.client.post(f"/api/cameras/{c1}/revoke")
    env.client.post("/api/outputs/receivers", json={"name": "Hall TV"})
    env.report(c2, LIVE_1080)
    rec_token = env.ctx.outputs.receivers()[0].token
    with env.ctx.db.session() as s:
        secrets = [i.token for i in s.query(Invitation)] + [r.stream_id for r in s.query(RetiredStream)]
    secrets += [c.stream_id for c in env.ctx.cameras.cameras()]
    secrets += [env.ctx.local_key, env.ctx.cameras.production().vdo_password, rec_token,
                "admin-pw-xyz", "obs-pw-xyz"]
    r = env.client.get("/api/diagnostics")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert "diagnostics.json" in z.namelist() and any(n.startswith("logs/") for n in z.namelist())
    blob = "\n".join(z.read(n).decode("utf-8", errors="replace") for n in z.namelist())
    for sec in secrets:
        assert sec not in blob, f"secret leaked: {sec[:6]}..."
    assert env.remote().get("/api/diagnostics").status_code == 401


# -- outputs ------------------------------------------------------------------------------------------------------
def test_receiver_lifecycle(env):
    r = env.client.post("/api/outputs/receivers", json={"name": "Hall TV"})
    assert r.status_code == 200
    rv = r.json()["receivers"][0]
    tok = env.ctx.outputs.receivers()[0].token
    assert rv["link"] == f"http://192.168.1.10:8000/tv/{tok}" and rv["state"] == "never_connected"
    rem = env.remote()
    assert rem.get(f"/api/public/tv/{tok}").json() == {"name": "Hall TV"}
    assert rem.post(f"/api/public/tv/{tok}/stats", json={"state": "playing", "width": 1920}).status_code == 200
    v = env.client.get("/api/outputs").json()["receivers"][0]
    assert v["online"] and v["state"] == "playing" and v["width"] == 1920 and v["rtt_ms"] is None
    # MediaMTX not running -> WHEP proxy says 503
    w = rem.post(f"/api/public/tv/{tok}/whep", content=b"v=0\r\n")
    assert w.status_code == 503
    assert env.client.delete(f"/api/outputs/receivers/{rv['id']}").status_code == 200
    assert rem.get(f"/api/public/tv/{tok}").status_code == 403
    assert rem.post(f"/api/public/tv/{tok}/whep", content=b"v=0").status_code == 403
    assert rem.post(f"/api/public/tv/{tok}/stats", json={"state": "playing"}).status_code == 403
    assert env.client.get("/api/outputs").json()["receivers"] == []


def test_bad_receiver_kind(env):
    assert env.client.post("/api/outputs/receivers", json={"kind": "carrier-pigeon"}).status_code == 400


def test_mediamtx_config(env_factory):
    env = env_factory(admin_password="admin-pw-xyz")
    env.client.post("/api/outputs/receivers", json={"name": "TV"})
    tok = env.ctx.outputs.receivers()[0].token
    cfg = env.ctx.outputs.mtx_config()
    assert "webrtcAdditionalHosts: [192.168.1.10]" in cfg
    assert tok not in cfg and "admin-pw-xyz" not in cfg
    assert "ips: ['127.0.0.1', '::1']" in cfg
    assert "apiAddress: 127.0.0.1:9997" in cfg


# -- metrics -------------------------------------------------------------------------------------------------------
def test_metrics_sample_unknowns_are_none(env):
    env.create_production(2)
    m = env.run(env.ctx.metrics.sample)
    for sec in ("system", "obs", "cameras", "outputs", "recording"):
        assert isinstance(m[sec]["ts"], float), sec
    assert m["ts"] > 0
    c = m["cameras"]
    assert c["registered"] == 2 and c["connected"] == 0
    assert c["avg_fps"] is None and c["in_kbps"] is None and c["avg_resolution"] is None
    assert c["max_packet_loss_pct"] is None
    assert m["outputs"]["latency_rtt_ms"] is None and m["outputs"]["mediamtx"] == "stopped"
    assert m["recording"]["estimated_remaining_s"] is None
    assert m["obs"]["connected"] is True and m["obs"]["simulated"] is True
    assert m["network"]["lan_ip"] == "192.168.1.10" and env.ctx.metrics.latest is m


def test_metrics_with_live_camera(env):
    env.create_production(2)
    c1, _ = env.cam_ids()
    env.report(c1, LIVE_1080)
    m = env.run(env.ctx.metrics.sample)
    c = m["cameras"]
    assert c["connected"] == 1 and c["avg_fps"] == 30.0 and c["in_kbps"] == 5800.0
    assert c["avg_resolution"] == "1920x1080" and c["below_1080p"] == 0
    h = env.client.get("/api/metrics/history").json()
    assert h and h[-1]["cams"] == 1


def test_network_health_uses_current_sample(env):
    env.create_production(2)
    env.report(env.cam_ids()[0], LIVE_1080)
    m = env.run(env.ctx.metrics.sample)
    assert m["network"]["health"] == "degraded"      # 1 of 2 cameras live


def test_tv_output_stop_restores_default_stream_service(env):
    """Real OBS 33 rejects SetStreamServiceSettings with empty settings; its untouched default is
    rtmp_common/{}. Stopping the TV output must still put that back (found on the real PC)."""
    out = env.ctx.outputs
    out._saved_service = {"streamServiceType": "rtmp_common", "streamServiceSettings": {}}
    env.mock.service = {"streamServiceType": "whip_custom",
                        "streamServiceSettings": {"server": "http://127.0.0.1:8889/festcast/whip", "bearer_token": ""}}
    env.run(out.stop_programme)
    assert env.mock.service["streamServiceType"] == "rtmp_common"
    assert env.mock.service["streamServiceSettings"] == {"key": ""}
    assert not any(n["level"] == "error" for n in env.ctx.bus.notifications)


def test_manual_is_served(env):
    r = env.client.get("/manual")
    assert r.status_code == 200 and "FestCast manual" in r.text and "Camera operator sheet" in r.text
