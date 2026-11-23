"""Build a self-contained FestCast release for the OS this runs on (Windows, Linux or macOS).

    python packaging/build.py --python-dir <folder with a python-build-standalone CPython> [--version 1.2.3]

Result: dist/FestCast-<version>-<target>.zip containing

    FestCast/
      start.bat, start-simulation.bat            (Windows)
      start.sh, start-simulation.sh,              (Linux, macOS)
      start.command, start-simulation.command     (macOS: double-click in Finder)
      python/              standalone CPython + FestCast's dependencies (no Python install needed)
      backend/, frontend/dist/, receiver/         (dashboard built; no Node needed)
      bin/mediamtx[.exe]   pinned MediaMTX, checksum-verified, with its MIT licence (bin/LICENSE-mediamtx.txt)
      LICENSE, THIRD-PARTY-NOTICES.txt, README.txt, .env.example

The CPython comes from python-build-standalone (relocatable builds, the same ones `uv python install` uses); the
GitHub workflow installs it with uv and passes the folder here. Needs frontend/dist built first (npm run build).
Only the standard library is used, so any Python 3.11+ can run this script."""
from __future__ import annotations

import argparse
import hashlib
import io
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tomllib
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
MEDIAMTX_VERSION = "v1.21.4"          # the version verified on the show PC; bump deliberately
DEV_ONLY = re.compile(r"^\s*(pytest|pytest-asyncio)\b", re.I)


def target() -> tuple[str, str, str]:
    """(release name, MediaMTX os, MediaMTX arch) for this machine."""
    system = platform.system()
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "amd64" if machine in ("x86_64", "amd64") else machine
    if system == "Windows":
        return ("windows" if arch == "amd64" else f"windows-{arch}"), "windows", arch
    if system == "Darwin":
        return ("macos-arm64" if arch == "arm64" else "macos-x64"), "darwin", arch
    if system == "Linux":
        return ("linux-arm64" if arch == "arm64" else "linux-x64"), "linux", arch
    raise SystemExit(f"unsupported platform {system} {machine}")


def run(*args: str | Path, **kw) -> None:
    print("+", " ".join(map(str, args)), flush=True)
    subprocess.run([str(a) for a in args], check=True, **kw)


def run_py(py: Path, *args: str | Path) -> None:
    """Run the bundled Python isolated (-E: no PYTHON* variables, -s: no user site-packages), so nothing from the
    build machine's own Python can be picked up or counted as already installed."""
    run(py, "-E", "-s", *args)


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "festcast-release-build"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


# -- pieces --------------------------------------------------------------------------------------------------------
def copy_python(src_root: Path, dest: Path) -> Path:
    """Copy a standalone CPython (the folder holding cpython-3.12.*, or that folder itself) to dest; return its exe."""
    cands = [src_root] if (src_root / "python.exe").exists() or (src_root / "bin").is_dir() else \
        sorted(p for p in src_root.glob("cpython-3.12.*") if p.is_dir() and not p.is_symlink()
               and not (hasattr(p, "is_junction") and p.is_junction()))   # uv also adds a cpython-3.12-* link
    if not cands:
        raise SystemExit(f"no cpython-3.12* folder in {src_root}")
    shutil.copytree(cands[-1], dest, symlinks=True)
    for marker in dest.rglob("EXTERNALLY-MANAGED"):      # this copy is ours: pip may install into it
        marker.unlink()
    exe = dest / "python.exe" if os.name == "nt" else dest / "bin" / "python3"
    if not exe.exists():
        raise SystemExit(f"python executable not found at {exe}")
    return exe


def install_deps(py: Path, work: Path) -> None:
    req = work / "requirements-runtime.txt"
    lines = (ROOT / "requirements.txt").read_text("utf-8").splitlines()
    req.write_text("\n".join(l for l in lines if not DEV_ONLY.match(l)) + "\n", "utf-8")
    run_py(py, "-m", "ensurepip", "--upgrade")
    run_py(py, "-m", "pip", "install", "--no-warn-script-location", "--disable-pip-version-check", "-r", req)
    run_py(py, "-c", "import fastapi, uvicorn, sqlalchemy, alembic, psutil, pypdfium2, PIL, cryptography, zeroconf; "
                  "print('dependencies import OK')")


def get_mediamtx(os_name: str, arch: str, bin_dir: Path) -> None:
    base = f"https://github.com/bluenviron/mediamtx/releases/download/{MEDIAMTX_VERSION}"
    sums = fetch(f"{base}/checksums.sha256").decode()
    pat = re.compile(rf"^([0-9a-f]{{64}})\s+\*?(mediamtx_{re.escape(MEDIAMTX_VERSION)}_{os_name}_{arch}(v8)?"
                     rf"\.(zip|tar\.gz))$", re.M)
    m = pat.search(sums)
    if not m:
        raise SystemExit(f"no MediaMTX {MEDIAMTX_VERSION} build for {os_name}/{arch} in checksums.sha256")
    want, asset = m.group(1), m.group(2)
    print(f"downloading {asset}", flush=True)
    data = fetch(f"{base}/{asset}")
    got = hashlib.sha256(data).hexdigest()
    if got != want:
        raise SystemExit(f"checksum mismatch for {asset}: {got} != {want}")
    bin_dir.mkdir(parents=True, exist_ok=True)
    exe_name = "mediamtx.exe" if os_name == "windows" else "mediamtx"
    if asset.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            (bin_dir / exe_name).write_bytes(z.read(exe_name))
            (bin_dir / "LICENSE-mediamtx.txt").write_bytes(z.read("LICENSE"))
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
            (bin_dir / exe_name).write_bytes(t.extractfile(exe_name).read())          # type: ignore[union-attr]
            (bin_dir / "LICENSE-mediamtx.txt").write_bytes(t.extractfile("LICENSE").read())  # type: ignore[union-attr]
    (bin_dir / exe_name).chmod(0o755)
    print(f"MediaMTX {MEDIAMTX_VERSION} ({asset}) sha256 {got} verified", flush=True)


def copy_app(app: Path) -> None:
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")
    shutil.copytree(ROOT / "backend", app / "backend", ignore=ignore)
    if not (ROOT / "frontend" / "dist" / "index.html").exists():
        raise SystemExit("frontend/dist is missing: run `npm ci && npm run build` in frontend/ first")
    shutil.copytree(ROOT / "frontend" / "dist", app / "frontend" / "dist")
    shutil.copytree(ROOT / "receiver", app / "receiver")
    for f in ("LICENSE", ".env.example"):
        shutil.copy2(ROOT / f, app / f)


def launchers(app: Path, name: str) -> None:
    src = HERE / "launchers"
    files = ["start.bat", "start-simulation.bat"] if name.startswith("windows") else ["start.sh", "start-simulation.sh"]
    if name.startswith("macos"):
        files += ["start.command", "start-simulation.command"]
    for f in files:
        real = f.replace(".command", ".sh")
        text = (src / real).read_text("utf-8")
        nl = "\r\n" if f.endswith(".bat") else "\n"
        (app / f).write_bytes(text.replace("\r\n", "\n").replace("\n", nl).encode("utf-8"))
        if not f.endswith(".bat"):
            (app / f).chmod(0o755)


def readme(app: Path, version: str, name: str) -> None:
    text = (HERE / "README.txt").read_text("utf-8").replace("{version}", version).replace("{target}", name)
    (app / "README.txt").write_text(text, "utf-8", newline="\r\n" if name.startswith("windows") else "\n")


def notices(app: Path, py: Path) -> None:
    out = app / "THIRD-PARTY-NOTICES.txt"
    run_py(py, HERE / "notices.py", out, app / "python", app / "bin" / "LICENSE-mediamtx.txt", MEDIAMTX_VERSION)


def make_zip(app: Path, dest: Path) -> None:
    """Zip keeping Unix permissions and symlinks (python/bin/python3 -> python3.12), which unzip and macOS restore."""
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in sorted(app.parent.joinpath(app.name).rglob("*")):
            rel = p.relative_to(app.parent).as_posix()
            st = p.lstat()
            if p.is_symlink():
                zi = zipfile.ZipInfo(rel)
                zi.create_system = 3
                zi.external_attr = (stat.S_IFLNK | 0o777) << 16
                z.writestr(zi, os.readlink(p))
            elif p.is_dir():
                continue
            else:
                zi = zipfile.ZipInfo.from_file(p, rel)
                zi.compress_type = zipfile.ZIP_DEFLATED
                if os.name != "nt":
                    zi.create_system = 3
                    zi.external_attr = (st.st_mode & 0xFFFF) << 16
                with open(p, "rb") as fh:
                    z.writestr(zi, fh.read(), compresslevel=9)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python-dir", type=Path, required=True,
                    help="python-build-standalone install (e.g. the UV_PYTHON_INSTALL_DIR used for `uv python install`)")
    ap.add_argument("--version", default=tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]["version"])
    ap.add_argument("--out", type=Path, default=ROOT / "dist")
    a = ap.parse_args()
    version = a.version.removeprefix("v")
    name, mtx_os, mtx_arch = target()
    work = a.out / f"build-{name}"
    if work.exists():
        shutil.rmtree(work)
    app = work / "FestCast"
    app.mkdir(parents=True)
    py = copy_python(a.python_dir, app / "python")
    install_deps(py, work)
    get_mediamtx(mtx_os, mtx_arch, app / "bin")
    copy_app(app)
    launchers(app, name)
    readme(app, version, name)
    notices(app, py)
    dest = a.out / f"FestCast-{version}-{name}.zip"
    dest.unlink(missing_ok=True)
    make_zip(app, dest)
    print(f"built {dest} ({dest.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
