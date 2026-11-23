"""Pure unit tests: stats parsing, colours, element cleaning, VDO.Ninja URLs, stream ids."""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from backend.database.models import Camera, RetiredStream
from backend.services import cameras as cam_mod
from backend.services.cameras import parse_vdo_stats
from backend.services.layouts import clean_element, hex_to_abgr
from backend.transports.vdoninja import VdoNinjaInputAdapter, new_stream_id

FIELDS = {"width", "height", "fps", "bitrate_kbps", "jitter_ms", "packet_loss_pct", "rtt_ms", "audio_kbps", "codec"}


# -- parse_vdo_stats ---------------------------------------------------------------------------------------
def test_parse_stats_primary_spelling():
    out = parse_vdo_stats({"Resolution": "1920 x 1080", "_FPS": 30, "video_bitrate_kbps": 5800,
                           "video_jitter_ms": 4.5, "average_roundTripTime_ms": 22, "audio_bitrate_kbps": 96,
                           "video_codec": "h264", "packetLoss_in_percentage": "0.5%"})
    assert out == {"width": 1920, "height": 1080, "fps": 30.0, "bitrate_kbps": 5800.0, "jitter_ms": 4.5,
                   "packet_loss_pct": 0.5, "rtt_ms": 22.0, "audio_kbps": 96.0, "codec": "h264"}


def test_parse_stats_alternate_spelling():
    out = parse_vdo_stats({"resolution": "1280×720", "fps": "29.97", "videoBitrate_kbps": "3000 kbps"})
    assert (out["width"], out["height"]) == (1280, 720)
    assert out["fps"] == pytest.approx(29.97)
    assert out["bitrate_kbps"] == 3000.0


def test_parse_stats_framespersecond_and_nested_inbound():
    out = parse_vdo_stats({"streamABC": {"inbound": {"Resolution": "2560x1440", "framesPerSecond": 25,
                                                     "video_bitrate_kbps": 9000}}})
    assert (out["width"], out["height"], out["fps"], out["bitrate_kbps"]) == (2560, 1440, 25.0, 9000.0)


def test_parse_stats_missing_fields_are_none_not_zero():
    out = parse_vdo_stats({"Resolution": "1920x1080"})
    assert set(out) == FIELDS
    for k in FIELDS - {"width", "height"}:
        assert out[k] is None, k


@pytest.mark.parametrize("raw", [None, "x", 5, [], {}])
def test_parse_stats_garbage(raw):
    assert parse_vdo_stats(raw) == dict.fromkeys(FIELDS)


def test_parse_stats_bool_and_non_numeric_are_none():
    out = parse_vdo_stats({"_FPS": True, "video_bitrate_kbps": "n/a", "video_codec": 7})
    assert out["fps"] is None and out["bitrate_kbps"] is None and out["codec"] is None


# -- hex_to_abgr ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("h,alpha,expected", [
    ("#ff0000", 1.0, 0xFF0000FF),
    ("#00ff00", 1.0, 0xFF00FF00),
    ("0000ff", 1.0, 0xFFFF0000),
    ("#0f0", 1.0, 0xFF00FF00),
    ("#000000", 0.0, 0x00000000),
    ("#ffffff", 5.0, 0xFFFFFFFF),     # alpha clamped
    ("#zzzzzz", 1.0, 0xFFFFFFFF),     # invalid -> white
    ("", 1.0, 0xFFFFFFFF),
])
def test_hex_to_abgr(h, alpha, expected):
    assert hex_to_abgr(h, alpha) == expected


def test_hex_to_abgr_half_alpha():
    assert hex_to_abgr("#000000", 0.5) >> 24 == 127


# -- clean_element ----------------------------------------------------------------------------------------------
def test_clean_element_rejects_unknown_type():
    with pytest.raises(ValueError):
        clean_element({"type": "video"})


def test_clean_element_clamps():
    e = clean_element({"type": "camera", "id": "a-b/c!", "x": 9, "y": -9, "w": 0, "h": 99, "opacity": 3,
                       "fit": "bogus", "crop": {"l": 0.9, "t": -1, "r": 0.2}, "camera_id": "x" * 50})
    assert e["id"] == "abc"
    assert (e["x"], e["y"], e["w"], e["h"]) == (2.0, -1.0, 0.005, 3.0)
    assert e["opacity"] == 1.0 and e["fit"] == "fit"
    assert e["crop"] == {"l": 0.45, "t": 0.0, "r": 0.2, "b": 0.0}
    assert len(e["camera_id"]) == 32


def test_clean_element_bad_numbers_fall_back_to_defaults():
    e = clean_element({"type": "color", "x": "abc", "w": None})
    assert e["x"] == 0 and e["w"] == 0.25
    assert e["color"] == "#000000" and len(e["id"]) > 0


def test_clean_element_text_and_image():
    t = clean_element({"type": "text", "text": "x" * 900, "size": 5000})
    assert len(t["text"]) == 500 and t["size"] == 400 and t["font"] == "Arial"
    i = clean_element({"type": "image", "src": "../../win/sys32.png"})
    assert "/" not in i["src"] and "\\" not in i["src"]


# -- VDO.Ninja URLs ----------------------------------------------------------------------------------------------
def _cam(**kw):
    d = {"stream_id": "fcabc", "password": "p@ss word", "name": "Cam 1", "fps": 30, "resolution_target": "1080p",
         "bitrate_kbps": 6000}
    d.update(kw)
    return d


def _qs(url):
    return parse_qs(urlparse(url).query, keep_blank_values=True)


def test_push_url():
    url = VdoNinjaInputAdapter("https://vdo.ninja").publish_url(_cam())
    assert url.startswith("https://vdo.ninja/?")
    q = _qs(url)
    assert q["push"] == ["fcabc"] and q["password"] == ["p@ss word"]
    assert "webcam" in q and q["facing"] == ["rear"]
    assert q["width"] == ["1920"] and q["height"] == ["1080"]
    assert "view" not in q


@pytest.mark.parametrize("res,wh", [("720p", ("1280", "720")), ("1440p", ("2560", "1440"))])
def test_push_url_resolutions(res, wh):
    q = _qs(VdoNinjaInputAdapter().publish_url(_cam(resolution_target=res)))
    assert (q["width"][0], q["height"][0]) == wh


def test_push_url_auto_has_no_size():
    q = _qs(VdoNinjaInputAdapter().publish_url(_cam(resolution_target="auto")))
    assert "width" not in q and "height" not in q


def test_view_url():
    url = VdoNinjaInputAdapter().view_url(_cam(bitrate_kbps=4500))
    q = _qs(url)
    assert q["view"] == ["fcabc"] and q["videobitrate"] == ["4500"] and "cleanoutput" in q
    assert q["buffer"] == ["0"] and "lowlatency" in q
    assert q["password"] == ["p@ss word"] and "push" not in q


# -- stream ids ---------------------------------------------------------------------------------------------------
def test_new_stream_ids_unique():
    ids = {new_stream_id() for _ in range(500)}
    assert len(ids) == 500 and all(i.startswith("fc") and i.isalnum() for i in ids)


def test_unique_stream_id_skips_retired_and_in_use(bare_ctx, monkeypatch):
    with bare_ctx.db.session() as s:
        s.add(RetiredStream(stream_id="fcretired"))
    seq = iter(["fcretired", "fcretired", "fcfresh"])
    monkeypatch.setattr(cam_mod, "new_stream_id", lambda: next(seq))
    assert bare_ctx.cameras._unique_stream_id() == "fcfresh"


def test_revoked_stream_never_reused(env):
    env.create_production(1)
    cid = env.cam_ids()[0]
    old = env.ctx.cameras.camera(cid).stream_id
    assert env.client.post(f"/api/cameras/{cid}/revoke").status_code == 200
    with env.ctx.db.session() as s:
        assert s.get(RetiredStream, old) is not None
        assert s.query(Camera).filter(Camera.stream_id == old).first() is None
    assert env.ctx.cameras.camera(cid).stream_id != old


def test_obs_log_audio_buffering(tmp_path):
    from backend.services.metrics import ObsLogWatch
    old = tmp_path / "2026-01-01 10-00-00.txt"
    old.write_text("adding 960 milliseconds of audio buffering, total audio buffering is now 960 milliseconds "
                   "(source: Old)\n", encoding="utf-8")
    w = ObsLogWatch(tmp_path)
    assert w.check() == (960, "Old")
    import os
    import time
    new = tmp_path / "2026-01-02 10-00-00.txt"
    new.write_text("OBS starting\n", encoding="utf-8")
    t = time.time() + 5
    os.utime(new, (t, t))
    assert w.check() == (0, None)                       # OBS restarted: fresh log, no buffering
    with open(new, "a", encoding="utf-8") as f:
        f.write("adding 21 milliseconds of audio buffering, total audio buffering is now 21 milliseconds "
                "(source: Desktop Audio)\n")
    os.utime(new, (t + 1, t + 1))
    assert w.check() == (21, "Desktop Audio")
    assert ObsLogWatch(tmp_path / "missing").check() == (None, None)
