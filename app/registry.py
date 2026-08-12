"""Tool registry: scan the tools dir, parse tool.yml manifests, suggest defaults."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

import yaml
from pydantic import ValidationError

from .models import TOOL_ID_RE, Manifest

log = logging.getLogger(__name__)

MANIFEST_FILE = "tool.yml"

# Dirs that are never tools
_IGNORED_DIRS = {".staging", ".venv", "node_modules", "__pycache__", ".git", "lost+found"}

# Generated/dependency dirs to skip when walking *inside* a single tool's own
# folder (file listing, the dev-mode watcher's change fingerprint).
NOISE_DIR_NAMES = {
    ".git", ".venv", "venv", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build",
}


class RegistryEntry:
    __slots__ = ("id", "path", "manifest", "error")

    def __init__(self, id: str, path: Path, manifest: Manifest | None, error: str | None):
        self.id = id
        self.path = path
        self.manifest = manifest  # None => unconfigured or broken
        self.error = error  # parse/validation error text, if any


def scan(tools_dir: Path) -> dict[str, RegistryEntry]:
    """Scan tools_dir for tool folders. Never raises on bad manifests."""
    entries: dict[str, RegistryEntry] = {}
    if not tools_dir.is_dir():
        return entries
    for child in sorted(tools_dir.iterdir()):
        if not child.is_dir() or child.name.startswith(".") or child.name in _IGNORED_DIRS:
            continue
        if not TOOL_ID_RE.match(child.name):
            log.warning("skipping tool dir with invalid name: %s", child.name)
            continue
        manifest, error = load_manifest(child)
        entries[child.name] = RegistryEntry(child.name, child, manifest, error)
    return entries


def load_manifest(tool_dir: Path) -> tuple[Manifest | None, str | None]:
    mf = tool_dir / MANIFEST_FILE
    if not mf.is_file():
        return None, None  # unconfigured
    try:
        raw = yaml.safe_load(mf.read_text(encoding="utf-8", errors="replace"))
        if not isinstance(raw, dict):
            return None, "tool.yml is not a YAML mapping"
        return Manifest.model_validate(raw), None
    except yaml.YAMLError as e:
        return None, f"YAML parse error: {e}"
    except ValidationError as e:
        msgs = "; ".join(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors())
        return None, f"invalid manifest: {msgs}"


def save_manifest(tool_dir: Path, manifest: Manifest) -> None:
    """Atomic write of tool.yml."""
    data = manifest.model_dump(exclude_none=True, exclude_defaults=True)
    data["run"] = manifest.run  # always keep run even if it matched a default
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    tmp = tool_dir / (MANIFEST_FILE + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, tool_dir / MANIFEST_FILE)


def suggest_manifest(tool_dir: Path) -> Manifest:
    """Best-effort manifest draft from what's in the folder."""
    name = tool_dir.name.replace("-", " ").replace("_", " ").title()
    # a symlinked tool dir means it was *linked* rather than copied in — i.e.
    # someone's actively developing it in place, so default to watch mode
    watch = tool_dir.is_symlink()

    req = tool_dir / "requirements.txt"
    pyproject = tool_dir / "pyproject.toml"
    pkg = tool_dir / "package.json"

    if req.is_file() or pyproject.is_file():
        deps = ""
        if req.is_file():
            deps = req.read_text(encoding="utf-8", errors="replace").lower()
        elif pyproject.is_file():
            deps = pyproject.read_text(encoding="utf-8", errors="replace").lower()

        entry = _first_existing(tool_dir, ["app.py", "main.py", "server.py", "run.py"]) or "app.py"
        install = (
            "uv pip install -r requirements.txt" if req.is_file() else "uv pip install -e ."
        )
        env: dict[str, str] = {}

        if "streamlit" in deps:
            run = f"streamlit run {entry} --server.port {{port}} --server.address 0.0.0.0 --server.headless true"
            health = {"type": "http", "path": "/", "timeout_s": 120}
        elif "gradio" in deps:
            run = f"python {entry}"
            env = {"GRADIO_SERVER_NAME": "0.0.0.0", "GRADIO_SERVER_PORT": "{port}"}
            health = {"type": "http", "path": "/", "timeout_s": 120}
        elif "fastapi" in deps or "uvicorn" in deps:
            module = Path(entry).stem
            run = f"uvicorn {module}:app --host 0.0.0.0 --port {{port}}"
            health = {"type": "http", "path": "/", "timeout_s": 60}
        elif "flask" in deps:
            module = Path(entry).stem
            env = {"FLASK_APP": entry}
            run = f"flask --app {module} run --host 0.0.0.0 --port {{port}}"
            health = {"type": "http", "path": "/", "timeout_s": 60}
        else:
            run = f"python {entry}"
            health = {"type": "tcp", "timeout_s": 60}

        return Manifest(
            name=name, type="python", install=install, run=run, env=env, health=health, watch=watch
        )

    if pkg.is_file():
        install = "npm ci" if (tool_dir / "package-lock.json").is_file() else "npm install"
        run = "npm start"
        try:
            meta = json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
            scripts = meta.get("scripts") or {}
            if "start" not in scripts:
                main = meta.get("main") or "index.js"
                run = f"node {main}"
        except (json.JSONDecodeError, OSError):
            pass
        return Manifest(name=name, type="node", install=install, run=run, watch=watch)

    # Unknown layout — give the user something editable
    return Manifest(name=name, type="custom", run="./start.sh", health={"type": "tcp"}, watch=watch)


def list_files(tool_dir: Path) -> list[str]:
    """Relative posix paths of every real file under tool_dir, skipping generated
    /dependency noise — used by the files API so an AI agent without local
    filesystem access can see what's in a tool's folder."""
    out: list[str] = []
    for root, dirs, files in os.walk(tool_dir):
        dirs[:] = [d for d in dirs if d not in NOISE_DIR_NAMES and not d.startswith(".")]
        for name in files:
            if name.startswith("."):
                continue
            out.append((Path(root) / name).relative_to(tool_dir).as_posix())
    return sorted(out)


def _first_existing(base: Path, names: list[str]) -> str | None:
    for n in names:
        if (base / n).is_file():
            return n
    return None


def substitute(template: str, *, port: int, tool_dir: Path) -> str:
    """Replace {port} and {dir} placeholders in commands and env values."""
    return template.replace("{port}", str(port)).replace("{dir}", str(tool_dir))
