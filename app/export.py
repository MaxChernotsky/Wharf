"""Tool export: build a zip of a tool's folder, excluding heavy/rebuildable dirs."""

from __future__ import annotations

import logging
import uuid
import zipfile
from pathlib import Path

log = logging.getLogger(__name__)

EXCLUDED_DIRS = {".venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache"}


def build_export_zip(tool_dir: Path, staging_dir: Path, *, include_git: bool = False) -> Path:
    """Write <staging>/<uuid>/<tool>.zip and return its path."""
    excluded = set(EXCLUDED_DIRS) if include_git else EXCLUDED_DIRS | {".git"}
    out_dir = staging_dir / f"export-{uuid.uuid4().hex}"
    out_dir.mkdir(parents=True)
    zip_path = out_dir / f"{tool_dir.name}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(tool_dir.rglob("*")):
            rel = path.relative_to(tool_dir)
            if any(part in excluded for part in rel.parts):
                continue
            if path.is_symlink() or not path.is_file():
                continue
            zf.write(path, Path(tool_dir.name) / rel)
    return zip_path
