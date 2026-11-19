"""Automatic port selection so FestCast runs on any PC / network without editing config.

A port counts as free only if nothing answers on 127.0.0.1 AND we can bind it on all interfaces: on Windows a
0.0.0.0 bind can succeed while another program owns 127.0.0.1:<port>, and OBS (which uses 127.0.0.1) would then
reach that program instead of FestCast. The chosen port is remembered so links/QR codes stay the same next run."""
from __future__ import annotations

import json
import socket
import sys
from pathlib import Path


def tcp_free(port: int) -> bool:
    if not 0 < port < 65536:
        return False
    with socket.socket() as probe:
        probe.settimeout(0.3)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            return False
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def udp_free(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if sys.platform == "win32":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def os_assigned(kind: int = socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("0.0.0.0", 0))
        return s.getsockname()[1]


def pick(preferred: list[int | None], scan: range, udp: bool = False, avoid: set[int] | None = None) -> int:
    check = udp_free if udp else tcp_free
    avoid = avoid or set()
    seen: set[int] = set()
    for p in [*preferred, *scan]:
        if p and p not in seen and p not in avoid:
            seen.add(p)
            if check(p):
                return p
    while True:
        p = os_assigned(socket.SOCK_DGRAM if udp else socket.SOCK_STREAM)
        if p not in avoid:
            return p


def remembered(data_dir: Path) -> dict:
    try:
        return json.loads((data_dir / "ports.json").read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def remember(data_dir: Path, **ports: int) -> None:
    d = remembered(data_dir)
    d.update(ports)
    try:
        (data_dir / "ports.json").write_text(json.dumps(d), "utf-8")
    except OSError:
        pass
