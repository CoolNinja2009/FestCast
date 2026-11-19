"""Automatic port selection (FestCast has to run on any PC / network)."""
from __future__ import annotations

import socket

from backend.core import ports as P


def _listen_loopback() -> tuple[socket.socket, int]:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    return s, s.getsockname()[1]


def test_loopback_listener_counts_as_busy():
    # the real-world case: another program owns 127.0.0.1:<port> while 0.0.0.0:<port> could still be bound
    s, port = _listen_loopback()
    try:
        assert not P.tcp_free(port)
        picked = P.pick([port], range(0))
        assert picked != port and P.tcp_free(picked)
    finally:
        s.close()


def test_preference_order_and_avoid():
    a = P.os_assigned()
    b = P.os_assigned()
    assert P.pick([a, b], range(0)) == a
    assert P.pick([a, b], range(0), avoid={a}) == b


def test_remembered_port_roundtrip(tmp_path):
    assert P.remembered(tmp_path) == {}
    P.remember(tmp_path, port=8421)
    P.remember(tmp_path, mtx_ice=8190)
    assert P.remembered(tmp_path) == {"port": 8421, "mtx_ice": 8190}


def test_mediamtx_ports_chosen_and_in_config(env):
    out = env.ctx.outputs
    out.choose_ports()
    cfg = out.mtx_config()
    p = out.ports
    assert f"webrtcAddress: :{p['webrtc']}" in cfg and f"webrtcLocalUDPAddress: :{p['ice']}" in cfg
    assert f"apiAddress: 127.0.0.1:{p['api']}" in cfg
    assert len({p["webrtc"], p["ice"], p["api"], env.ctx.settings.port}) == 4
    assert P.remembered(env.ctx.settings.data_dir)["mtx_webrtc"] == p["webrtc"]


def test_obs_sources_follow_a_changed_port(env):
    """If FestCast had to move to another port, the OBS browser sources must be re-pointed on (re)connect."""
    env.client.post("/api/production", json={"name": "P", "cameras": 1}).raise_for_status()
    cam = env.client.get("/api/state").json()["cameras"]["cameras"][0]
    env.ctx.settings.port = 8999
    env.run(env.ctx.cameras.sync_all)
    env.run(env.ctx.production.ensure_standby)
    url = env.mock.inputs[cam["obs_input"]]["settings"]["url"]
    assert url.startswith("http://127.0.0.1:8999/obs/cam/")
    assert env.mock.inputs["FC_STANDBY_main"]["settings"]["url"].startswith("http://127.0.0.1:8999/obs/standby")
