"""Start OBS together with FestCast, in FestCast's own OBS profile and scene collection, and hand the user's own
OBS back untouched when that OBS closes.

OBS remembers the last used profile/collection in user.ini (OBS 31+; global.ini before) and would open the show
setup next time. So before launching we save those [Basic] keys, and a small detached watcher (this file run as a
script, stdlib only) waits for OBS to exit and writes them back. The watcher is double-spawned so it and OBS are
not children of FestCast: closing the start.bat window or restart.bat (taskkill /T) never takes OBS down.
If the watcher itself dies, the saved state stays in data/obs_session.json and the next FestCast start restores it.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

log = logging.getLogger("festcast.obs.launcher")

KEYS = ("Profile", "ProfileDir", "SceneCollection", "SceneCollectionFile")
EXE_NAMES = {"obs64.exe", "obs.exe", "obs"}
# keep the isolation the launchers start Python with (-E -s) in the helper processes too
PYFLAGS = [*(["-E"] if sys.flags.ignore_environment else []), *(["-s"] if sys.flags.no_user_site else [])]


def config_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", "")) / "obs-studio"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "obs-studio"
    native = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "obs-studio"
    flatpak = Path.home() / ".var" / "app" / "com.obsproject.Studio" / "config" / "obs-studio"
    return flatpak if flatpak.is_dir() and not native.is_dir() else native


def find_obs(configured: str = "") -> str | None:
    if configured and Path(configured).exists():
        return configured
    cands: list[Path] = []
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\OBS Studio") as k:
                cands.append(Path(winreg.QueryValue(k, None)) / "bin" / "64bit" / "obs64.exe")
        except OSError:
            pass
        cands.append(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "obs-studio/bin/64bit/obs64.exe")
    elif sys.platform == "darwin":
        cands += [Path("/Applications/OBS.app/Contents/MacOS/OBS"),
                  Path.home() / "Applications/OBS.app/Contents/MacOS/OBS"]
    for c in cands:
        if c.exists():
            return str(c)
    return shutil.which("obs")


def obs_running() -> bool:
    import psutil
    for p in psutil.process_iter(["name"]):
        if (p.info.get("name") or "").lower() in EXE_NAMES:
            return True
    return False


# -- ini (line based, so OBS's file stays exactly as OBS wrote it) --------------------------------------------------
def _read(path: Path) -> tuple[list[str], bool]:
    raw = path.read_bytes() if path.exists() else b""
    bom = raw.startswith(b"\xef\xbb\xbf")
    return raw.decode("utf-8-sig", errors="replace").splitlines(), bom


def ini_get(path: Path, section: str, keys=KEYS) -> dict[str, str]:
    lines, _ = _read(path)
    out, cur = {}, None
    for ln in lines:
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
            cur = s[1:-1]
        elif cur == section and "=" in s:
            k, v = s.split("=", 1)
            if k.strip() in keys:
                out[k.strip()] = v.strip()
    return out


def ini_set(path: Path, section: str, values: dict[str, str]) -> None:
    lines, bom = _read(path)
    todo, out, cur, seen = dict(values), [], None, False

    def flush() -> None:
        out.extend(f"{k}={v}" for k, v in todo.items())
        todo.clear()

    for ln in lines:
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
            if cur == section:
                flush()
            cur = s[1:-1]
            seen = seen or cur == section
        elif cur == section and "=" in s and s.split("=", 1)[0].strip() in todo:
            k = s.split("=", 1)[0].strip()
            ln = f"{k}={todo.pop(k)}"
        out.append(ln)
    if cur == section:
        flush()
    if not seen and todo:
        out += [f"[{section}]"] + [f"{k}={v}" for k, v in todo.items()]
    text = "\n".join(out) + "\n"
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))


def user_ini(cfg: Path) -> Path:
    """OBS 31+ keeps the last used profile/collection in user.ini, older versions in global.ini."""
    u = cfg / "user.ini"
    return u if u.exists() or not (cfg / "global.ini").exists() else cfg / "global.ini"


# -- FestCast's profile and collection -------------------------------------------------------------------------------
def _profile_dir(cfg: Path, name: str) -> Path | None:
    for d in sorted((cfg / "basic" / "profiles").glob("*")):
        if ini_get(d / "basic.ini", "General", ("Name",)).get("Name") == name:
            return d
    return None


def ensure_profile(cfg: Path, name: str) -> Path:
    """FestCast's OBS profile. Created once as a copy of the user's current profile (their video and encoder
    settings), without their stream service/key."""
    d = _profile_dir(cfg, name)
    if d:
        return d
    root = cfg / "basic" / "profiles"
    d = root / name
    i = 2
    while d.exists():
        d, i = root / f"{name}{i}", i + 1
    src = root / ini_get(user_ini(cfg), "Basic").get("ProfileDir", "")
    d.mkdir(parents=True)
    if src != root and (src / "basic.ini").exists():
        for f in src.iterdir():
            if f.is_file() and not f.name.endswith(".bak") and not f.name.startswith("service"):
                shutil.copy2(f, d / f.name)
    ini_set(d / "basic.ini", "General", {"Name": name})
    log.info("created OBS profile %s (%s)", name, d)
    return d


def _collection_exists(cfg: Path, name: str) -> bool:
    for f in (cfg / "basic" / "scenes").glob("*.json"):
        try:
            if json.loads(f.read_text(encoding="utf-8-sig")).get("name") == name:
                return True
        except (OSError, ValueError):
            continue
    return False


def ensure_collection(cfg: Path, name: str) -> None:
    """FestCast's OBS scene collection: like a new collection made in OBS (one empty scene, default desktop audio
    and mic). FestCast adds its own scenes and sources on connect."""
    if _collection_exists(cfg, name):
        return
    d = cfg / "basic" / "scenes"
    d.mkdir(parents=True, exist_ok=True)
    f, i = d / f"{name}.json", 2
    while f.exists():
        f, i = d / f"{name}{i}.json", i + 1
    scene = {"id": "scene", "versioned_id": "scene", "name": "Scene", "uuid": str(uuid.uuid4()),
             "settings": {"id_counter": 0, "custom_size": False, "items": []}}
    data = {"name": name, "current_scene": "Scene", "current_program_scene": "Scene",
            "scene_order": [{"name": "Scene"}], "sources": [scene], "groups": [], "transitions": [],
            "current_transition": "Fade", "transition_duration": 300, "quick_transitions": [],
            "saved_projectors": [], "preview_locked": False, "scaling_enabled": False, "modules": {}}
    devices = {"nt": (("DesktopAudioDevice1", "wasapi_output_capture", "Desktop Audio"),
                      ("AuxAudioDevice1", "wasapi_input_capture", "Mic/Aux")),
               "darwin": (("AuxAudioDevice1", "coreaudio_input_capture", "Mic/Aux"),),
               "linux": (("DesktopAudioDevice1", "pulse_output_capture", "Desktop Audio"),
                         ("AuxAudioDevice1", "pulse_input_capture", "Mic/Aux"))}
    plat = "nt" if os.name == "nt" else "darwin" if sys.platform == "darwin" else "linux"
    for key, kind, label in devices[plat]:
        data[key] = {"id": kind, "versioned_id": kind, "name": label, "uuid": str(uuid.uuid4()),
                     "settings": {"device_id": "default"}, "mixers": 255, "volume": 1.0}
    f.write_text(json.dumps(data, indent=4), encoding="utf-8")
    log.info("created OBS scene collection %s (%s)", name, f)


# -- restore ---------------------------------------------------------------------------------------------------------
def restore(state_file: Path) -> bool:
    """Give the user's OBS back its own last used profile/collection. True if something was restored."""
    try:
        st = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if st.get("saved"):
        ini_set(Path(st["ini"]), "Basic", st["saved"])
    state_file.unlink(missing_ok=True)
    return True


def restore_pending(state_file: Path) -> None:
    """A watcher that died (PC crash) leaves its state behind: restore once OBS is no longer running."""
    if state_file.exists() and not obs_running() and restore(state_file):
        log.info("restored the user's OBS profile/collection left from a previous run")


def ensure_obs(exe_setting: str, profile: str, collection: str, state_file: Path) -> str:
    """Start OBS for FestCast unless one is already running. Returns a short status for the log."""
    restore_pending(state_file)
    if obs_running():
        return "already running"
    exe = find_obs(exe_setting)
    if not exe:
        return "not found (set FESTCAST_OBS_PATH)"
    cfg = config_dir()
    ensure_profile(cfg, profile)
    ensure_collection(cfg, collection)
    ini = user_ini(cfg)
    cur = ini_get(ini, "Basic")
    ours = {"Profile": profile, "SceneCollection": collection}
    if cur.get("Profile") == profile or cur.get("SceneCollection") == collection:
        cur = {}   # already pointing at FestCast's (a restore went wrong earlier): nothing of the user's to save
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"ini": str(ini), "saved": cur, "ours": ours}), encoding="utf-8")
    args = [sys.executable, *PYFLAGS, str(Path(__file__).resolve()), "spawn", exe, profile, collection, str(state_file)]
    _detached(args)
    return f"started ({exe}, profile/collection '{profile}'/'{collection}')"


def _detached(args: list[str]) -> None:
    kw: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                "close_fds": True}
    if os.name == "nt":
        kw["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(args, **kw)


def _watch(exe: str, profile: str, collection: str, state_file: Path) -> None:
    p = subprocess.Popen([exe, "--profile", profile, "--collection", collection, "--disable-shutdown-check"],
                         cwd=str(Path(exe).parent))   # OBS finds its data files relative to the working folder
    p.wait()
    time.sleep(2)    # OBS writes user.ini as it exits
    restore(state_file)


if __name__ == "__main__":
    mode, *rest = sys.argv[1:]
    if mode == "spawn":      # first hop: start the watcher and exit, so it has no living parent
        _detached([sys.executable, *PYFLAGS, str(Path(__file__).resolve()), "watch", *rest])
    elif mode == "watch":
        _watch(rest[0], rest[1], rest[2], Path(rest[3]))
