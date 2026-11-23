"""Write THIRD-PARTY-NOTICES.txt for a release: run with the bundled Python, so it lists exactly what is shipped.

    python notices.py <out file> <bundled python folder> <MediaMTX licence file> <MediaMTX version>"""
from __future__ import annotations

import sys
from importlib import metadata
from pathlib import Path

out, py_root, mtx_licence, mtx_version = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
RULE = "=" * 100


def licence_texts(dist: metadata.Distribution) -> list[str]:
    texts = []
    for f in dist.files or []:
        name = f.name.upper()
        if any(name.startswith(k) for k in ("LICENSE", "LICENCE", "COPYING", "NOTICE", "AUTHORS")) \
                and ".dist-info" in str(f):
            try:
                text = Path(dist.locate_file(f)).read_text(encoding="utf-8", errors="replace")
                texts.append(f"--- {f.name} ---\n" + text.strip())
            except OSError:
                pass
    return texts


parts = [
    "FestCast is free software under the GNU General Public License v3 (see LICENSE).",
    "This package also contains the third-party software below, each under its own licence.",
    "Not included (install separately if you use them): OBS Studio (GPL-2.0), ffmpeg (LGPL/GPL), LibreOffice (MPL-2.0).",
    "",
]

py_lic = next((p for p in [py_root / "LICENSE.txt", *py_root.glob("lib/python3*/LICENSE.txt"),
                           *py_root.glob("**/LICENSE.txt")] if p.is_file()), None)
parts += [RULE, f"Python {sys.version.split()[0]} (python-build-standalone build of CPython)",
          "Licence: Python Software Foundation License (and the licences of its bundled libraries, below)", RULE,
          py_lic.read_text("utf-8", errors="replace").strip() if py_lic else "(licence file not found in this build)", ""]

parts += [RULE, f"MediaMTX {mtx_version} (bin/)  https://github.com/bluenviron/mediamtx", "Licence: MIT", RULE,
          mtx_licence.read_text("utf-8", errors="replace").strip(), ""]

seen = set()
for dist in sorted(metadata.distributions(), key=lambda d: (d.metadata["Name"] or "").lower()):
    name = dist.metadata["Name"] or "?"
    if name.lower() in seen:
        continue
    seen.add(name.lower())
    lic = dist.metadata.get("License-Expression") or dist.metadata.get("License") or ""
    if len(lic) > 120 or "\n" in lic:
        lic = lic.splitlines()[0][:120] + " ..."
    classifiers = [c.split("::")[-1].strip() for c in (dist.metadata.get_all("Classifier") or [])
                   if c.startswith("License ::")]
    home = dist.metadata.get("Home-page") or next(
        (u.split(",", 1)[-1].strip() for u in (dist.metadata.get_all("Project-URL") or [])), "")
    parts += [RULE, f"{name} {dist.version}  {home}".rstrip(),
              f"Licence: {lic or ', '.join(classifiers) or 'see below'}", RULE]
    parts += licence_texts(dist) or ["(no licence file shipped in the package metadata)"]
    parts.append("")

out.write_text("\n".join(parts) + "\n", "utf-8")
print(f"wrote {out} ({len(seen)} Python packages)")
