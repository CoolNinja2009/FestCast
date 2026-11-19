"""Local camera transport: phone --WebRTC (WHIP)--> MediaMTX on this PC --WebRTC (WHEP)--> OBS browser source.

No internet and no relay: signalling goes phone -> FestCast (token-checked, /api/public/cam/<token>/whip) -> MediaMTX
on loopback, and the media goes straight from the phone to MediaMTX's ICE port on the LAN. The phone page is
/cam/<token> on FestCast's https listener (browsers only open the camera on https pages). The OBS page
(/obs/cam/<id>) plays the camera from MediaMTX with WHEP. Each camera publishes to its own MediaMTX path
cam_<stream_id>; revoking rotates the stream id, so an old phone link publishes into a path nobody reads."""
from __future__ import annotations

from .vdoninja import CameraInputAdapter


def mtx_path(cam: dict) -> str:
    return f"cam_{cam['stream_id']}"


class LocalInputAdapter(CameraInputAdapter):
    transport = "local"

    def publish_url(self, cam: dict) -> str | None:
        return None     # the phone page URL needs the invitation token; built by CameraService.publish_link

    def obs_source(self, cam: dict, wrapper_url: str) -> tuple[str, dict]:
        return "browser_source", {"url": wrapper_url, "width": cam.get("canvas_w", 1920),
                                  "height": cam.get("canvas_h", 1080), "fps_custom": True, "fps": cam.get("fps", 30),
                                  "reroute_audio": True, "shutdown": False, "restart_when_active": False}
