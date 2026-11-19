"""Production control (programme/preview/transitions) and the standby engine.

Nothing here switches the programme unexpectedly: automatic standby only fires on rules the operator enabled,
never because one camera dropped while another is still live, and returning from standby is manual unless
`auto_return` is switched on."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING

from ..database.models import Production, now
from ..obs.client import ObsError

if TYPE_CHECKING:
    from ..core.context import Ctx

log = logging.getLogger("festcast.production")

STANDBY_SCENE = "FC Standby"
STANDBY_INPUT = "FC_STANDBY_main"

DEFAULT_RULES = {"when_not_started": False, "when_no_cameras": True, "grace_seconds": 15, "auto_return": False,
                 "schedule": []}  # schedule: [{"start":"HH:MM","end":"HH:MM","label":"Lunch"}]


class ProductionService:
    def __init__(self, ctx: "Ctx") -> None:
        self.ctx = ctx
        self.override = "auto"          # auto | forced_on | forced_off
        self.return_scene: str | None = None
        self.no_cam_since: float | None = None
        self.standby_reason: str | None = None
        self._task: asyncio.Task | None = None

    @property
    def rules(self) -> dict:
        return {**DEFAULT_RULES, **(self.ctx.db.get("standby_rules") or {})}

    def set_rules(self, rules: dict) -> dict:
        r = self.rules
        for k in DEFAULT_RULES:
            if k in rules:
                r[k] = rules[k]
        r["grace_seconds"] = max(0, min(int(r["grace_seconds"]), 600))
        self.ctx.db.set("standby_rules", r)
        self.publish()
        return r

    # -- OBS scene setup ---------------------------------------------------------------
    async def ensure_standby(self) -> None:
        obs = self.ctx.obs
        await obs.ensure_scene(STANDBY_SCENE, "standby", "standby")
        url = f"http://127.0.0.1:{self.ctx.settings.port}/obs/standby?k={self.ctx.local_key}"
        p = self.ctx.cameras.production()
        await obs.ensure_input(STANDBY_INPUT, "browser_source", {
            "url": url, "width": p.canvas_w if p else 1920, "height": p.canvas_h if p else 1080,
            "fps_custom": True, "fps": 30, "reroute_audio": True, "shutdown": False}, STANDBY_SCENE, "standby",
            "standby")
        await obs.fit_full(STANDBY_SCENE, STANDBY_INPUT)

    # -- state ---------------------------------------------------------------------------------------
    def started(self) -> bool:
        p = self.ctx.cameras.production()
        return bool(p and p.started_at)

    async def start_production(self) -> dict:
        p = self.ctx.cameras.production()
        if not p:
            raise ValueError("Create a production first")
        with self.ctx.db.session() as s:
            s.get(Production, p.id).started_at = now()
        log.info("production started")
        self.ctx.bus.notify("info", "Production started", "production")
        rec = self.ctx.recording.config
        if rec.get("auto_start") and self.ctx.obs.connected and not self.ctx.obs.state.get("recording"):
            try:
                await self.ctx.recording.start()
            except Exception as e:  # noqa: BLE001
                self.ctx.bus.notify("error", f"Automatic recording start failed: {e}", "recording")
        self.ctx.cameras.publish()
        self.publish()
        return self.view()

    async def end_production(self) -> dict:
        p = self.ctx.cameras.production()
        if p:
            with self.ctx.db.session() as s:
                s.get(Production, p.id).started_at = None
        log.info("production ended")
        self.ctx.cameras.publish()
        self.publish()
        return self.view()

    # -- switching ------------------------------------------------------------------------------------
    async def set_transition(self, name: str | None, duration_ms: int | None) -> None:
        if name:
            await self.ctx.obs.call("SetCurrentSceneTransition", {"transitionName": name})
        if duration_ms is not None:
            await self.ctx.obs.call("SetCurrentSceneTransitionDuration",
                                    {"transitionDuration": max(50, min(int(duration_ms), 20000))})

    async def preview(self, scene: str) -> None:
        obs = self.ctx.obs
        if not obs.state.get("studio_mode"):
            await obs.call("SetStudioModeEnabled", {"studioModeEnabled": True})
        await obs.call("SetCurrentPreviewScene", {"sceneName": scene})

    async def take(self, transition: str | None = None, duration_ms: int | None = None) -> None:
        """Preview -> programme with the chosen transition (Cut/Fade/...)."""
        obs = self.ctx.obs
        if not obs.state.get("studio_mode") or not obs.state.get("preview_scene"):
            raise ValueError("Nothing in preview")
        await self.set_transition(transition, duration_ms)
        await obs.call("TriggerStudioModeTransition")
        log.info("take -> %s (%s)", obs.state.get("preview_scene"), transition or obs.state.get("transition"))

    async def go_live(self, scene: str, transition: str | None = None, duration_ms: int | None = None) -> None:
        """Directly put a scene on programme (used by quick-cut buttons)."""
        obs = self.ctx.obs
        await self.set_transition(transition, duration_ms)
        if obs.state.get("studio_mode"):
            await obs.call("SetCurrentPreviewScene", {"sceneName": scene})
            await obs.call("TriggerStudioModeTransition")
        else:
            await obs.call("SetCurrentProgramScene", {"sceneName": scene})
        log.info("programme -> %s", scene)

    async def activate_standby(self, reason: str = "manual") -> None:
        obs = self.ctx.obs
        await self.ensure_standby()
        cur = obs.state.get("program_scene")
        if cur and cur != STANDBY_SCENE:
            self.return_scene = cur
        await self.go_live(STANDBY_SCENE, "Fade" if "Fade" in obs.state.get("transitions", []) else None)
        self.standby_reason = reason
        if reason == "manual":
            self.override = "forced_on"
        self.ctx.bus.notify("warning" if reason != "manual" else "info",
                            f"Standby ON ({reason}). Use 'Return to production' to go back.", "standby")
        if obs.state.get("recording") and not self.ctx.recording.config.get("record_during_standby", True):
            await self.ctx.recording.pause_for_standby()
        self.publish()

    async def return_to_production(self, scene: str | None = None) -> None:
        target = scene or self.return_scene
        if not target:
            raise ValueError("Choose a scene to return to")
        await self.go_live(target, "Fade" if "Fade" in self.ctx.obs.state.get("transitions", []) else None)
        self.override = "forced_off" if self.rules.get("when_no_cameras") else "auto"
        self.standby_reason = None
        self.no_cam_since = None
        await self.ctx.recording.resume_after_standby()
        self.ctx.bus.notify("info", f"Back to production: {target}", "standby")
        self.publish()

    def set_override(self, mode: str) -> None:
        if mode not in ("auto", "forced_on", "forced_off"):
            raise ValueError("bad override")
        self.override = mode
        self.publish()

    # -- automatic standby engine -----------------------------------------------------------------------
    def _in_schedule(self) -> dict | None:
        hm = datetime.now().strftime("%H:%M")
        for s in self.rules.get("schedule") or []:
            if s.get("start", "") <= hm < s.get("end", ""):
                return s
        return None

    def desired_standby(self) -> str | None:
        """Return a reason if the rules say standby should be on, else None."""
        r = self.rules
        if self.override == "forced_off":
            return None
        sch = self._in_schedule()
        if sch:
            return f"scheduled intermission: {sch.get('label') or 'break'}"
        if r["when_not_started"] and self.ctx.cameras.production() and not self.started():
            return "production not started"
        if r["when_no_cameras"] and self.started() and self.ctx.cameras.cameras():
            if self.ctx.cameras.usable_count() == 0:
                self.no_cam_since = self.no_cam_since or time.time()
                if time.time() - self.no_cam_since >= r["grace_seconds"]:
                    return "no usable camera feeds"
            else:
                self.no_cam_since = None
        return None

    async def evaluate(self) -> None:
        obs = self.ctx.obs
        if not obs.connected:
            return
        on_standby = obs.state.get("program_scene") == STANDBY_SCENE
        reason = self.desired_standby()
        if reason and not on_standby and self.override != "forced_off":
            try:
                await self.activate_standby(reason)
            except (ObsError, ValueError) as e:
                self.ctx.bus.notify("error", f"Automatic standby failed: {e}", "standby")
        elif (not reason and on_standby and self.standby_reason and self.standby_reason != "manual"
              and self.rules["auto_return"] and self.return_scene):
            try:
                await self.return_to_production()
            except (ObsError, ValueError) as e:
                self.ctx.bus.notify("error", f"Automatic return failed: {e}", "standby")
        if self.override == "forced_off" and not reason and self.ctx.cameras.usable_count() > 0:
            self.override = "auto"   # cameras are back: re-arm the rules

    async def run(self) -> None:
        while True:
            try:
                await self.evaluate()
            except Exception:  # noqa: BLE001
                log.exception("standby evaluation failed")
            await asyncio.sleep(2)

    def view(self) -> dict:
        obs = self.ctx.obs.state
        return {"started": self.started(), "program_scene": obs.get("program_scene"),
                "preview_scene": obs.get("preview_scene"), "studio_mode": obs.get("studio_mode"),
                "transition": obs.get("transition"), "transitions": obs.get("transitions"),
                "standby_active": obs.get("program_scene") == STANDBY_SCENE, "standby_reason": self.standby_reason,
                "override": self.override, "return_scene": self.return_scene, "rules": self.rules,
                "standby_scene": STANDBY_SCENE}

    def publish(self) -> None:
        self.ctx.bus.publish("production", self.view())
