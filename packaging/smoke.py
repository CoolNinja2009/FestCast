"""Smoke-test a release zip the way a user gets it: unzip it (with the system unzip on Linux/macOS, so permissions
and symlinks are restored as they would be), start the bundled FestCast in simulation, and check that the dashboard,
the manual and the bundled MediaMTX are found. Exits non-zero on any failure.

    python packaging/smoke.py dist/FestCast-1.2.3-linux-x64.zip"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.read()
    except OSError:
        return 0, b""


def main() -> None:
    zip_path = Path(sys.argv[1]).resolve()
    tmp = Path(tempfile.mkdtemp(prefix="festcast-smoke-"))
    try:
        if os.name == "nt":
            zipfile.ZipFile(zip_path).extractall(tmp)
        else:
            subprocess.run(["unzip", "-q", str(zip_path), "-d", str(tmp)], check=True)
        app = tmp / "FestCast"
        py = app / ("python/python.exe" if os.name == "nt" else "python/bin/python3")
        launcher = app / ("start.bat" if os.name == "nt" else "start.sh")
        for f in (py, launcher, app / "LICENSE", app / "THIRD-PARTY-NOTICES.txt", app / "README.txt",
                  app / "frontend/dist/index.html"):
            assert f.exists(), f"missing {f}"
        if os.name != "nt":
            assert os.access(py, os.X_OK) and os.access(launcher, os.X_OK), "launcher / python not executable"
        port = free_port()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("FESTCAST_", "PYTHON", "VIRTUAL_ENV"))}
        proc = subprocess.Popen([str(py), "-E", "-s", "-m", "backend.main", "--simulation", "--port", str(port),
                                 "--data-dir", str(tmp / "data-smoke")], cwd=app, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            base = f"http://127.0.0.1:{port}"
            end = time.time() + 90
            while time.time() < end and get(base + "/")[0] != 200:
                if proc.poll() is not None:
                    raise AssertionError("FestCast exited:\n" + proc.stdout.read().decode(errors="replace"))  # type: ignore[union-attr]
                time.sleep(1)
            status, body = get(base + "/")
            assert status == 200 and b"<html" in body.lower(), "dashboard not served"
            assert get(base + "/manual")[0] == 200, "manual not served"
            outputs = json.loads(get(base + "/api/outputs")[1] or b"{}")
            assert outputs.get("mediamtx", {}).get("found"), f"bundled MediaMTX not found: {outputs.get('mediamtx')}"
            print(f"smoke test OK: {zip_path.name} serves the dashboard and finds its MediaMTX")
        finally:
            proc.terminate()
            try:
                proc.wait(15)
            except subprocess.TimeoutExpired:
                proc.kill()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
