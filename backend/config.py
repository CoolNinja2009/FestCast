"""FestCast configuration (environment / .env driven, prefix FESTCAST_)."""
from __future__ import annotations

import shutil
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FESTCAST_", env_file=str(ROOT / ".env"), extra="ignore")

    host: str = "0.0.0.0"
    # 0 = automatic: last used port, else 8420, else the next free one (see core/ports.py)
    port: int = 0
    data_dir: Path = ROOT / "data"
    # Dashboard access from other machines needs this password. Requests from this PC (127.0.0.1) are always allowed.
    admin_password: str = ""
    # Address phones / TVs use to reach FestCast. Empty = auto-detect the LAN IP.
    public_host: str = ""

    # OBS WebSocket v5 (credentials stay server side)
    obs_host: str = "127.0.0.1"
    obs_port: int = 4455
    obs_password: str = ""
    # simulation = in-memory mock OBS; never mixed with a real connection
    simulation: bool = False

    vdo_base: str = "https://vdo.ninja/"
    invite_ttl_hours: int = 48

    mediamtx_path: str = ""          # auto: tools/mediamtx/mediamtx(.exe) or PATH
    mediamtx_webrtc_port: int = 8889
    mediamtx_stream_path: str = "festcast"

    ffmpeg_path: str = ""
    soffice_path: str = ""

    metrics_interval: float = 2.0
    preview_interval: float = 1.5

    @property
    def db_path(self) -> Path:
        return self.data_dir / "festcast.db"

    def tool(self, name: str, configured: str, extra: list[Path] | None = None) -> str | None:
        if configured and Path(configured).exists():
            return configured
        for p in extra or []:
            if p.exists():
                return str(p)
        return shutil.which(name)

    @property
    def ffmpeg(self) -> str | None:
        return self.tool("ffmpeg", self.ffmpeg_path)

    @property
    def soffice(self) -> str | None:
        return self.tool("soffice", self.soffice_path, [
            Path(r"C:\Program Files\LibreOffice\program\soffice.exe"),
            Path("/usr/bin/soffice"), Path("/usr/bin/libreoffice")])

    @property
    def mediamtx(self) -> str | None:
        return self.tool("mediamtx", self.mediamtx_path, [
            ROOT / "tools" / "mediamtx" / "mediamtx.exe", ROOT / "tools" / "mediamtx" / "mediamtx"])


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s
