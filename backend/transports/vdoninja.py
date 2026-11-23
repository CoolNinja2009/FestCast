"""Camera input adapters.

VdoNinjaInputAdapter builds the documented URL-based push/view links (verified against vdo.ninja main.js, see
docs/ARCHITECTURE.md). There is no VDO.Ninja API that creates rooms: a stream exists as soon as a phone publishes
with `?push=ID`, and is pulled with `?view=ID`. Both sides share a per-production `password`, which VDO.Ninja uses to
encrypt/obscure the stream ID, so a guessed stream ID alone is useless.

We deliberately do NOT put the phones in a VDO.Ninja `room`: room guests also pull each other's video, which would
waste the phones' uplink on school Wi-Fi. The FestCast production (with its room_id) is the logical room.
"""
from __future__ import annotations

import secrets
from abc import ABC, abstractmethod
from urllib.parse import quote

RESOLUTIONS = {"720p": (1280, 720), "1080p": (1920, 1080), "1440p": (2560, 1440)}


def new_stream_id() -> str:
    # VDO.Ninja stream IDs are alphanumeric; 20 chars of [a-z0-9] ~ 103 bits
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "fc" + "".join(secrets.choice(alphabet) for _ in range(20))


def new_password() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(16))


class CameraInputAdapter(ABC):
    transport: str

    @abstractmethod
    def publish_url(self, cam: dict) -> str | None: ...

    @abstractmethod
    def obs_source(self, cam: dict, wrapper_url: str) -> tuple[str, dict]:
        """(inputKind, inputSettings) for the OBS input representing this camera."""


class VdoNinjaInputAdapter(CameraInputAdapter):
    transport = "vdoninja"

    def __init__(self, base: str = "https://vdo.ninja/") -> None:
        self.base = base if base.endswith("/") else base + "/"

    def publish_url(self, cam: dict) -> str:
        p = [f"push={cam['stream_id']}", f"password={quote(cam['password'])}", f"label={quote(cam['name'])}",
             "webcam", "facing=rear", f"fps={cam.get('fps', 30)}"]
        res = RESOLUTIONS.get(cam.get("resolution_target", "1080p"))
        if res:
            p += [f"width={res[0]}", f"height={res[1]}"]
        return self.base + "?" + "&".join(p)

    def view_url(self, cam: dict) -> str:
        # videobitrate is requested by the viewer (kbps); cleanoutput hides all VDO.Ninja UI for OBS.
        # Latency (docs.vdo.ninja): buffer=0 asks for the smallest WebRTC jitter buffer; lowlatency is the
        # documented preset (no audio buffer/sync node, 10 ms audio packets, no web-audio). Viewer-side only.
        return (self.base + f"?view={cam['stream_id']}&password={quote(cam['password'])}"
                f"&videobitrate={int(cam.get('bitrate_kbps', 6000))}&cleanoutput&buffer=0&lowlatency")

    def obs_source(self, cam: dict, wrapper_url: str) -> tuple[str, dict]:
        return "browser_source", {"url": wrapper_url, "width": cam.get("canvas_w", 1920),
                                  "height": cam.get("canvas_h", 1080), "fps_custom": True, "fps": cam.get("fps", 30),
                                  "reroute_audio": True, "shutdown": False, "restart_when_active": False}


ADAPTERS: dict[str, CameraInputAdapter] = {}


def adapter_for(transport: str, base: str) -> CameraInputAdapter:
    if transport == "vdoninja":
        return VdoNinjaInputAdapter(base)
    raise ValueError(f"unsupported camera transport {transport!r}")
