"""Persistent configuration (SQLite). Transient runtime state (live stats, OBS status) is NOT stored here."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class Production(Base):
    __tablename__ = "productions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), default="FestCast Production")
    room_id: Mapped[str] = mapped_column(String(64), unique=True)
    vdo_password: Mapped[str] = mapped_column(String(64))
    canvas_w: Mapped[int] = mapped_column(Integer, default=1920)
    canvas_h: Mapped[int] = mapped_column(Integer, default=1080)
    fps: Mapped[int] = mapped_column(Integer, default=30)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    cameras: Mapped[list["Camera"]] = relationship(back_populates="production", cascade="all, delete-orphan",
                                                   order_by="Camera.slot")


class Camera(Base):
    __tablename__ = "cameras"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    production_id: Mapped[str] = mapped_column(ForeignKey("productions.id"))
    slot: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(80))
    input_type: Mapped[str] = mapped_column(String(32), default="phone")
    transport: Mapped[str] = mapped_column(String(32), default="vdoninja")
    stream_id: Mapped[str] = mapped_column(String(64), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    resolution_target: Mapped[str] = mapped_column(String(16), default="1080p")
    bitrate_kbps: Mapped[int] = mapped_column(Integer, default=6000)
    audio_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    production: Mapped[Production] = relationship(back_populates="cameras")


class RetiredStream(Base):
    """Every stream id ever revoked: never handed out again."""
    __tablename__ = "retired_streams"
    stream_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    retired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Invitation(Base):
    __tablename__ = "invitations"
    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    camera_id: Mapped[str] = mapped_column(String(32), index=True)
    stream_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Receiver(Base):
    __tablename__ = "receivers"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(16), default="webrtc")  # webrtc | hdmi
    token: Mapped[str] = mapped_column(String(64), unique=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    monitor_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Layout(Base):
    __tablename__ = "layouts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(80))
    # elements: list of {id,type,x,y,w,h (normalised 0..1),z,locked,hidden,fit,crop{l,t,r,b},camera_id,text,...}
    elements: Mapped[list] = mapped_column(JSON, default=list)
    obs_scene: Mapped[str | None] = mapped_column(String(160), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Presentation(Base):
    __tablename__ = "presentations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120))
    original_file: Mapped[str] = mapped_column(String(400))
    slides: Mapped[list] = mapped_column(JSON, default=list)  # [{file, duration, enabled}]
    default_duration: Mapped[float] = mapped_column(Float, default=8.0)
    transition: Mapped[str] = mapped_column(String(16), default="fade")
    background: Mapped[str] = mapped_column(String(16), default="#000000")
    branding: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Recording(Base):
    __tablename__ = "recordings"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    path: Mapped[str | None] = mapped_column(String(600), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="recording")  # recording|finished|failed|interrupted
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    remux_path: Mapped[str | None] = mapped_column(String(600), nullable=True)


class ObsOwned(Base):
    """Ownership registry: FestCast only ever modifies / deletes OBS objects listed here."""
    __tablename__ = "obs_owned"
    name: Mapped[str] = mapped_column(String(200), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))   # input | scene
    role: Mapped[str] = mapped_column(String(32))   # camera | standby | graphic | layout | camscene
    ref_id: Mapped[str] = mapped_column(String(64), index=True)
    uuid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class KV(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSON)
