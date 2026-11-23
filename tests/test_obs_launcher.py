"""OBS autostart: FestCast's own profile/collection, and the user's last used ones given back."""
import json

from backend.obs import launcher as L

INI = "\ufeff[General]\nFirstRun=true\n\n[Basic]\nProfile=Mine\nProfileDir=Mine\nSceneCollection=Mine\nSceneCollectionFile=Mine.json\nConfigOnNewProfile=false\n"


def setup_cfg(tmp_path):
    cfg = tmp_path / "obs-studio"
    (cfg / "basic" / "profiles" / "Mine").mkdir(parents=True)
    (cfg / "basic" / "scenes").mkdir(parents=True)
    (cfg / "basic" / "profiles" / "Mine" / "basic.ini").write_text("[General]\nName=Mine\n\n[Video]\nBaseCX=2560\n")
    (cfg / "basic" / "profiles" / "Mine" / "service.json").write_text('{"settings":{"key":"secret"}}')
    (cfg / "basic" / "scenes" / "Mine.json").write_text('{"name":"Mine"}')
    (cfg / "user.ini").write_bytes(INI.encode("utf-8"))
    return cfg


def test_ini_roundtrip_keeps_file(tmp_path):
    cfg = setup_cfg(tmp_path)
    ini = cfg / "user.ini"
    assert L.ini_get(ini, "Basic")["SceneCollectionFile"] == "Mine.json"
    L.ini_set(ini, "Basic", {"Profile": "FestCast", "ProfileDir": "FestCast"})
    L.ini_set(ini, "Basic", {"Profile": "Mine", "ProfileDir": "Mine"})
    assert ini.read_bytes() == INI.encode("utf-8")


def test_profile_copies_settings_not_stream_key(tmp_path):
    cfg = setup_cfg(tmp_path)
    d = L.ensure_profile(cfg, "FestCast")
    assert L.ini_get(d / "basic.ini", "General", ("Name",)) == {"Name": "FestCast"}
    assert "BaseCX=2560" in (d / "basic.ini").read_text()
    assert not (d / "service.json").exists()
    assert L.ensure_profile(cfg, "FestCast") == d
    assert L.ini_get(cfg / "basic" / "profiles" / "Mine" / "basic.ini", "General", ("Name",))["Name"] == "Mine"


def test_collection_created_once(tmp_path):
    cfg = setup_cfg(tmp_path)
    L.ensure_collection(cfg, "FestCast")
    L.ensure_collection(cfg, "FestCast")
    files = sorted(p.name for p in (cfg / "basic" / "scenes").glob("*.json"))
    assert files == ["FestCast.json", "Mine.json"]
    data = json.loads((cfg / "basic" / "scenes" / "FestCast.json").read_text())
    assert data["name"] == "FestCast" and data["scene_order"] == [{"name": "Scene"}]


def test_restore_gives_back_users_choice(tmp_path):
    cfg = setup_cfg(tmp_path)
    ini = cfg / "user.ini"
    state = tmp_path / "obs_session.json"
    state.write_text(json.dumps({"ini": str(ini), "saved": L.ini_get(ini, "Basic")}))
    # what OBS writes when the FestCast instance exits
    L.ini_set(ini, "Basic", {"Profile": "FestCast", "ProfileDir": "FestCast", "SceneCollection": "FestCast",
                             "SceneCollectionFile": "FestCast.json"})
    assert L.restore(state)
    assert ini.read_bytes() == INI.encode("utf-8")
    assert not state.exists()
    assert not L.restore(state)
