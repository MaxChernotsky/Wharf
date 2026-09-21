"""Zip upload handling: staging, zip-slip protection, atomic install into tools dir."""

from __future__ import annotations

import logging
import os
import re
import shutil
import uuid
import zipfile
from pathlib import Path

from .config import Settings
from .models import TOOL_ID_RE

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 10 * 1024 * 1024  # a single source file has no business being bigger than this


class UploadError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def safe_join(base: Path, rel: str) -> Path:
    """Resolve `rel` under `base`, rejecting traversal and absolute paths."""
    clean = (rel or "").replace("\\", "/").strip("/")
    if not clean or ".." in Path(clean).parts:
        raise UploadError(f"unsafe path: {rel!r}")
    base_resolved = base.resolve()
    dest = (base / clean).resolve()
    if not dest.is_relative_to(base_resolved):
        raise UploadError(f"path escapes tool dir: {rel!r}")
    return dest


def sanitize_tool_id(name: str) -> str:
    base = Path(name).stem
    base = re.sub(r"[^a-zA-Z0-9._-]+", "-", base).strip("-._")
    if not base or not TOOL_ID_RE.match(base):
        raise UploadError(f"cannot derive a valid tool name from {name!r}")
    return base


def extract_zip(zip_path: Path, settings: Settings) -> Path:
    """Extract into a fresh staging dir with zip-slip/zip-bomb protection.

    Returns the directory containing the tool source (single top-level folder unwrapped).
    """
    staging = settings.staging_dir / uuid.uuid4().hex
    staging.mkdir(parents=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            members = zf.infolist()
            if len(members) > settings.upload_max_files:
                raise UploadError("zip contains too many files")
            total = sum(m.file_size for m in members)
            if total > settings.upload_max_extracted_bytes:
                raise UploadError("zip expands beyond the allowed size")

            staging_resolved = staging.resolve()
            for m in members:
                name = m.filename
                if not name or name.endswith("/"):
                    continue  # directories created implicitly
                # symlinks: external attributes high bytes carry the unix mode
                mode = (m.external_attr >> 16) & 0xF000
                if mode == 0xA000:
                    log.warning("skipping symlink member %s", name)
                    continue
                if Path(name).is_absolute() or name.startswith(("/", "\\")):
                    raise UploadError(f"zip member has absolute path: {name}")
                dest = (staging / name).resolve()
                if not dest.is_relative_to(staging_resolved):
                    raise UploadError(f"zip member escapes extraction dir: {name}")
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(m) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
    except zipfile.BadZipFile as e:
        shutil.rmtree(staging, ignore_errors=True)
        raise UploadError(f"not a valid zip file: {e}")
    except UploadError:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    # unwrap a single top-level folder (the common `zip -r tool.zip tool/` shape)
    entries = [p for p in staging.iterdir() if p.name != "__MACOSX"]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return staging


def install_tool_dir(src: Path, tool_id: str, settings: Settings, *, replace: bool = False) -> Path:
    """Atomically move an extracted tool into /data/tools/<tool_id>.

    A tool's `data/` subfolder is its own convention for persistent storage —
    on a replace (update), it's carried over instead of being wiped along
    with the rest of the old tool dir.
    """
    dest = settings.tools_dir / tool_id
    preserved_data = None
    if dest.exists():
        if not replace:
            raise UploadError(f"a tool named {tool_id!r} already exists", status_code=409)
        old_data = dest / "data"
        if old_data.is_dir() and not old_data.is_symlink():
            preserved_data = settings.staging_dir / f"{uuid.uuid4().hex}-data"
            shutil.move(str(old_data), str(preserved_data))
        shutil.rmtree(dest)
    os.replace(src, dest)  # same filesystem: staging lives under /data
    if preserved_data is not None:
        new_data = dest / "data"
        if new_data.is_symlink() or new_data.is_file():
            new_data.unlink()
        elif new_data.is_dir():
            shutil.rmtree(new_data)
        shutil.move(str(preserved_data), str(new_data))
    return dest


def stage_update(zip_path: Path, tool_id: str, settings: Settings) -> Path:
    """Extract a zip and park it as a pending update for an already-installed
    tool, without touching the live tool dir. Replaces any update already
    staged for this tool_id; the caller is responsible for confirming the
    tool actually exists first."""
    src = extract_zip(zip_path, settings)
    dest = settings.pending_updates_dir / tool_id
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dest)  # same filesystem: staging lives under /data
    cleanup_staging(src, settings)
    return dest


def has_pending_update(tool_id: str, settings: Settings) -> bool:
    return (settings.pending_updates_dir / tool_id).is_dir()


def discard_pending_update(tool_id: str, settings: Settings) -> None:
    shutil.rmtree(settings.pending_updates_dir / tool_id, ignore_errors=True)


def apply_pending_update(tool_id: str, settings: Settings) -> Path:
    """Install a previously staged update into the live tool dir — same
    replace-and-preserve-data/ path as install_tool_dir(replace=True), which
    moves the staged copy into place (nothing left to clean up after)."""
    pending = settings.pending_updates_dir / tool_id
    if not pending.is_dir():
        raise UploadError("no update staged for this tool", status_code=404)
    return install_tool_dir(pending, tool_id, settings, replace=True)


def link_tool_dir(tool_id: str, src: Path, settings: Settings) -> Path:
    """Symlink tools_dir/<tool_id> -> src instead of copying anything in —
    for active development, where the tool should keep living in the dev's
    own checkout and Wharf just runs what's there. `src` must already be
    visible to the Wharf process (a local path in dev, or a bind-mounted
    path when running in Docker)."""
    if not TOOL_ID_RE.match(tool_id):
        raise UploadError(f"invalid tool id: {tool_id!r}")
    expanded = src.expanduser()
    if not expanded.is_absolute():
        raise UploadError("path must be absolute")
    resolved = expanded.resolve()
    if not resolved.is_dir():
        raise UploadError(f"not a directory (or not visible to Wharf): {src}")
    dest = settings.tools_dir / tool_id
    if dest.exists() or dest.is_symlink():
        raise UploadError(f"a tool named {tool_id!r} already exists", status_code=409)
    dest.symlink_to(resolved, target_is_directory=True)
    return dest


def remove_tool_dir(tool_dir: Path) -> None:
    """Remove a tool's entry from tools_dir. A linked tool's dir is a
    symlink — only the link itself is removed; the folder it points to
    (the dev's own checkout) is never touched. shutil.rmtree refuses to
    operate on a symlink for exactly this reason, so it's split out here
    rather than trusted to do the right thing implicitly."""
    if tool_dir.is_symlink():
        tool_dir.unlink()
    else:
        shutil.rmtree(tool_dir, ignore_errors=True)


def cleanup_staging(path: Path, settings: Settings) -> None:
    """Remove the staging dir that contained `path`, if any remains."""
    try:
        rel = path.resolve().relative_to(settings.staging_dir.resolve())
        root = settings.staging_dir / rel.parts[0]
        shutil.rmtree(root, ignore_errors=True)
    except ValueError:
        pass


# -- single-file API: lets an AI agent build a tool through HTTP calls alone,
# with no local filesystem access to wherever Wharf's tools_dir actually lives --

def write_tool_file(tool_dir: Path, rel_path: str, content: str) -> Path:
    """Create or overwrite one text file inside a tool's folder."""
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        raise UploadError("file too large", status_code=413)
    dest = safe_join(tool_dir, rel_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    return dest


def read_tool_file(tool_dir: Path, rel_path: str) -> str:
    dest = safe_join(tool_dir, rel_path)
    if not dest.is_file():
        raise UploadError(f"no file at {rel_path!r}", status_code=404)
    try:
        return dest.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise UploadError("file is not valid UTF-8 text", status_code=415)


def delete_tool_file(tool_dir: Path, rel_path: str) -> None:
    dest = safe_join(tool_dir, rel_path)
    if not dest.is_file():
        raise UploadError(f"no file at {rel_path!r}", status_code=404)
    dest.unlink()


def create_tool_dir(tool_id: str, files: dict[str, str], settings: Settings) -> Path:
    """Scaffold a brand-new tool folder from inline {relpath: content} — the
    create path for an AI agent. Fails if the tool already exists; use
    write_tool_file for edits to an existing one."""
    if not TOOL_ID_RE.match(tool_id):
        raise UploadError(f"invalid tool id: {tool_id!r}")
    tool_dir = settings.tools_dir / tool_id
    if tool_dir.exists():
        raise UploadError(f"a tool named {tool_id!r} already exists", status_code=409)
    tool_dir.mkdir(parents=True)
    try:
        for rel, content in files.items():
            write_tool_file(tool_dir, rel, content)
    except UploadError:
        shutil.rmtree(tool_dir, ignore_errors=True)
        raise
    return tool_dir
