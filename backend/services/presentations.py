"""Standby presentations: import PDF / PPTX / images, render pages to PNG (in a worker thread), keep the original,
and drive the OBS standby browser source (/obs/standby)."""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from ..database.models import Presentation, new_id

if TYPE_CHECKING:
    from ..core.context import Ctx

log = logging.getLogger("festcast.presentations")
IMG = {".png", ".jpg", ".jpeg", ".webp"}
ALLOWED = IMG | {".pdf", ".pptx", ".ppt", ".odp"}
MAX_BYTES = 300 * 1024 * 1024


def safe_name(name: str) -> str:
    base = Path(name).name
    return re.sub(r"[^A-Za-z0-9._ -]", "_", base)[:120] or "file"


def render_pdf(pdf: Path, out: Path, width: int = 1920) -> list[str]:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(pdf))
    files = []
    try:
        for i in range(len(doc)):
            page = doc[i]
            scale = width / page.get_width()
            img = page.render(scale=scale).to_pil()
            f = f"slide_{i + 1:03d}.png"
            img.save(out / f, "PNG", optimize=False)
            files.append(f)
    finally:
        doc.close()
    return files


def pptx_to_pdf(soffice: str, src: Path, workdir: Path) -> Path:
    # LibreOffice headless conversion. Static rendering: PowerPoint animations/transitions are NOT preserved.
    subprocess.run([soffice, "--headless", "--norestore", "--convert-to", "pdf", "--outdir", str(workdir), str(src)],
                   check=True, timeout=300, capture_output=True)
    pdf = workdir / (src.stem + ".pdf")
    if not pdf.exists():
        raise RuntimeError("LibreOffice did not produce a PDF")
    return pdf


class PresentationService:
    def __init__(self, ctx: "Ctx") -> None:
        self.ctx = ctx
        self.root = ctx.settings.data_dir / "presentations"
        self.root.mkdir(parents=True, exist_ok=True)
        self.nonce = 0
        # manual control of the standby page: it reports the slide it shows (pos); a command is "go to slide N",
        # numbered by cmd_seq so the page acts on each one once; paused stops the slide timers
        self.pos: int | None = None
        self.paused = False
        self.cmd_seq = 0
        self.cmd_index = 0

    def slide_count(self) -> int:
        return len(self.player_state()["slides"])

    def control(self, action: str, index: int | None = None) -> dict:
        n = self.slide_count()
        if action == "pause":
            self.paused = True
        elif action == "play":
            self.paused = False
        elif action in ("next", "prev", "goto"):
            if not n:
                raise ValueError("No presentation with slides is on standby")
            cur = self.pos if self.pos is not None else 0
            if action == "goto":
                if index is None or not 0 <= int(index) < n:
                    raise ValueError(f"slide must be 1 to {n}")
                target = int(index)
            else:
                target = (cur + (1 if action == "next" else -1)) % n
            self.cmd_seq += 1
            self.cmd_index = target
            self.pos = target
        else:
            raise ValueError("action must be next, prev, goto, pause or play")
        self.publish()
        return self.player()

    def player(self) -> dict:
        n = self.slide_count()
        return {"pos": self.pos if self.pos is not None and self.pos < n else None, "count": n, "paused": self.paused}

    def reported(self, at: str | None, seq: str | None = None) -> None:
        """The standby page tells us which slide it is showing (ignored until it has applied the latest command, so
        a report from just before a Next can't undo it)."""
        if seq not in (None, "") and seq != str(self.cmd_seq):
            return
        try:
            pos = int(at) if at not in (None, "") else None
        except ValueError:
            return
        if pos is not None and pos < 0:
            pos = None
        if pos != self.pos:
            self.pos = pos
            self.publish()

    def all(self) -> list[Presentation]:
        with self.ctx.db.session() as s:
            return list(s.query(Presentation).order_by(Presentation.created_at))

    def get(self, pid: str) -> Presentation:
        with self.ctx.db.session() as s:
            p = s.get(Presentation, pid)
            if not p:
                raise KeyError(pid)
            return p

    async def import_files(self, name: str, files: list[tuple[str, bytes]]) -> Presentation:
        if not files:
            raise ValueError("no files")
        for fn, data in files:
            if Path(fn).suffix.lower() not in ALLOWED:
                raise ValueError(f"{fn}: unsupported type (PDF, PPTX, PNG, JPEG, WebP)")
            if len(data) > MAX_BYTES:
                raise ValueError(f"{fn}: file too large")
        pid = new_id()
        d = self.root / pid
        slides_dir = d / "slides"
        slides_dir.mkdir(parents=True)
        try:
            slides = await asyncio.to_thread(self._import_sync, d, slides_dir, files)
        except Exception:
            shutil.rmtree(d, ignore_errors=True)
            raise
        with self.ctx.db.session() as s:
            p = Presentation(id=pid, name=(name or Path(files[0][0]).stem)[:120],
                             original_file=", ".join(safe_name(f) for f, _ in files),
                             slides=[{"file": f, "duration": None, "enabled": True} for f in slides])
            s.add(p)
        log.info("presentation imported %s (%d slides)", p.name, len(slides))
        self.publish()
        return p

    def _import_sync(self, d: Path, slides_dir: Path, files: list[tuple[str, bytes]]) -> list[str]:
        orig = d / "original"
        orig.mkdir()
        out: list[str] = []
        for fn, data in sorted(files, key=lambda x: x[0].lower()):
            sn = safe_name(fn)
            src = orig / sn
            src.write_bytes(data)
            ext = src.suffix.lower()
            if ext == ".pdf":
                out += self._prefix(render_pdf(src, slides_dir), slides_dir, len(out))
            elif ext in (".pptx", ".ppt", ".odp"):
                soffice = self.ctx.settings.soffice
                if not soffice:
                    raise ValueError("PPTX import needs LibreOffice (soffice). Install it, or export the deck to PDF "
                                     "from PowerPoint (File > Save As > PDF) and import the PDF.")
                with tempfile.TemporaryDirectory() as td:
                    pdf = pptx_to_pdf(soffice, src, Path(td))
                    out += self._prefix(render_pdf(pdf, slides_dir), slides_dir, len(out))
            else:
                from PIL import Image
                with Image.open(src) as im:
                    im.verify()
                f = f"slide_{len(out) + 1:03d}{'.jpg' if ext == '.jpeg' else ext}"
                shutil.copyfile(src, slides_dir / f)
                out.append(f)
        return out

    @staticmethod
    def _prefix(files: list[str], d: Path, offset: int) -> list[str]:
        if not offset:
            return files
        res = []
        for f in files:
            nf = f"slide_{offset + len(res) + 1:03d}.png"
            (d / f).rename(d / ("tmp_" + nf))
            res.append(nf)
        for f in res:
            (d / ("tmp_" + f)).rename(d / f)
        return res

    def update(self, pid: str, data: dict) -> Presentation:
        with self.ctx.db.session() as s:
            p = s.get(Presentation, pid)
            if not p:
                raise KeyError(pid)
            if "name" in data:
                p.name = str(data["name"])[:120] or p.name
            if "default_duration" in data:
                p.default_duration = max(1.0, min(float(data["default_duration"]), 3600))
            if "transition" in data and data["transition"] in ("cut", "fade", "slide"):
                p.transition = data["transition"]
            if "background" in data and re.fullmatch(r"#[0-9a-fA-F]{6}", str(data["background"])):
                p.background = data["background"]
            if "branding" in data:
                p.branding = str(data["branding"])[:200]
            if "slides" in data:
                known = {sl["file"] for sl in p.slides}
                new = []
                for sl in data["slides"]:
                    if sl.get("file") in known:
                        dur = sl.get("duration")
                        new.append({"file": sl["file"], "enabled": bool(sl.get("enabled", True)),
                                    "duration": max(1.0, min(float(dur), 3600)) if dur else None})
                p.slides = new
        self.nonce += 1
        self.publish()
        return p

    def delete(self, pid: str) -> None:
        with self.ctx.db.session() as s:
            p = s.get(Presentation, pid)
            if not p:
                raise KeyError(pid)
            s.delete(p)
        shutil.rmtree(self.root / pid, ignore_errors=True)
        if self.ctx.db.get("active_presentation") == pid:
            self.ctx.db.set("active_presentation", None)
        self.nonce += 1
        self.publish()

    def activate(self, pid: str | None) -> None:
        if pid:
            self.get(pid)
        self.ctx.db.set("active_presentation", pid)
        self.nonce += 1
        self.pos = None
        self.publish()

    def slide_path(self, pid: str, file: str) -> Path | None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", file) or not re.fullmatch(r"[a-f0-9]{32}", pid):
            return None
        p = (self.root / pid / "slides" / file)
        return p if p.exists() else None

    def player_state(self) -> dict:
        """What the OBS standby page shows."""
        pid = self.ctx.db.get("active_presentation")
        prod = self.ctx.cameras.production()
        st = {"nonce": self.nonce, "event": prod.name if prod else "FestCast", "slides": [], "transition": "fade",
              "background": "#000000", "branding": "", "message": self.ctx.db.get("standby_message")
              or "We'll be right back", "paused": self.paused, "cmd_seq": self.cmd_seq, "cmd_index": self.cmd_index}
        if pid:
            try:
                p = self.get(pid)
            except KeyError:
                return st
            st.update(transition=p.transition, background=p.background, branding=p.branding,
                      slides=[{"url": f"/obs/slides/{p.id}/{sl['file']}",
                               "duration": sl.get("duration") or p.default_duration}
                              for sl in p.slides if sl.get("enabled", True)])
        return st

    def view(self, p: Presentation) -> dict:
        return {"id": p.id, "name": p.name, "original_file": p.original_file, "default_duration": p.default_duration,
                "transition": p.transition, "background": p.background, "branding": p.branding,
                "slides": [{**sl, "url": f"/api/presentations/{p.id}/slides/{sl['file']}"} for sl in p.slides],
                "active": self.ctx.db.get("active_presentation") == p.id}

    def snapshot(self) -> dict:
        return {"items": [self.view(p) for p in self.all()], "active": self.ctx.db.get("active_presentation"),
                "message": self.ctx.db.get("standby_message") or "We'll be right back",
                "pptx_supported": bool(self.ctx.settings.soffice), "player": self.player()}

    def publish(self) -> None:
        self.ctx.bus.publish("presentations", self.snapshot())
