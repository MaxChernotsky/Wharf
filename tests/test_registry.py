from pathlib import Path

from app import registry
from app.models import Manifest


def make_tool(tools_dir: Path, name: str, manifest: str | None = None, files: dict | None = None) -> Path:
    d = tools_dir / name
    d.mkdir(parents=True)
    if manifest is not None:
        (d / "tool.yml").write_text(manifest)
    for fname, content in (files or {}).items():
        path = d / fname
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return d


def test_scan_finds_valid_manifest(settings):
    make_tool(settings.tools_dir, "mytool", "run: python app.py\nname: My Tool\n")
    entries = registry.scan(settings.tools_dir)
    assert "mytool" in entries
    assert entries["mytool"].manifest.name == "My Tool"
    assert entries["mytool"].error is None


def test_scan_reports_broken_manifest_without_crashing(settings):
    make_tool(settings.tools_dir, "broken", "run: [unclosed\n")
    make_tool(settings.tools_dir, "missing-run", "name: no run here\n")
    make_tool(settings.tools_dir, "good", "run: echo hi\n")
    entries = registry.scan(settings.tools_dir)
    assert entries["broken"].manifest is None and entries["broken"].error
    assert entries["missing-run"].manifest is None and "run" in entries["missing-run"].error
    assert entries["good"].manifest is not None


def test_scan_skips_hidden_and_ignored_dirs(settings):
    (settings.tools_dir / ".staging").mkdir()
    (settings.tools_dir / "__pycache__").mkdir()
    make_tool(settings.tools_dir, "real", "run: echo hi\n")
    entries = registry.scan(settings.tools_dir)
    assert set(entries) == {"real"}


def test_unconfigured_dir_has_no_manifest_and_no_error(settings):
    make_tool(settings.tools_dir, "bare", None, {"app.py": "print('hi')"})
    entries = registry.scan(settings.tools_dir)
    assert entries["bare"].manifest is None
    assert entries["bare"].error is None


def test_suggest_streamlit(settings):
    d = make_tool(settings.tools_dir, "st-tool", None,
                  {"requirements.txt": "streamlit>=1.0\n", "app.py": ""})
    m = registry.suggest_manifest(d)
    assert m.type == "python"
    assert "streamlit run app.py" in m.run
    assert "{port}" in m.run and "0.0.0.0" in m.run


def test_suggest_node_with_start_script(settings):
    d = make_tool(settings.tools_dir, "node-tool", None,
                  {"package.json": '{"scripts": {"start": "node s.js"}}'})
    m = registry.suggest_manifest(d)
    assert m.type == "node"
    assert m.run == "npm start"
    assert m.install == "npm install"


def test_suggest_fastapi(settings):
    d = make_tool(settings.tools_dir, "api-tool", None,
                  {"requirements.txt": "fastapi\nuvicorn\n", "main.py": ""})
    m = registry.suggest_manifest(d)
    assert "uvicorn main:app" in m.run


def test_save_and_reload_manifest_roundtrip(settings):
    d = make_tool(settings.tools_dir, "rt", "run: echo original\n")
    m = Manifest(run="echo updated", name="RT", autostart=True)
    registry.save_manifest(d, m)
    loaded, err = registry.load_manifest(d)
    assert err is None
    assert loaded.run == "echo updated"
    assert loaded.autostart is True


def test_substitute():
    out = registry.substitute("run --port {port} --dir {dir}", port=8123, tool_dir=Path("/data/tools/x"))
    assert out == "run --port 8123 --dir /data/tools/x"


def test_suggest_manifest_defaults_watch_on_for_linked_folder(settings, tmp_path):
    src = tmp_path / "dev-tool"
    src.mkdir()
    (src / "requirements.txt").write_text("fastapi\n")
    (src / "app.py").write_text("")
    link = settings.tools_dir / "linked"
    link.symlink_to(src, target_is_directory=True)

    assert registry.suggest_manifest(link).watch is True
    assert registry.suggest_manifest(src).watch is False  # the real folder itself isn't a link


def test_list_files_skips_noise_dirs_and_dotfiles(settings):
    d = make_tool(settings.tools_dir, "listed", None, {"app.py": "x", "sub/b.py": "y"})
    (d / ".venv").mkdir()
    (d / ".venv" / "lib.py").write_text("z")
    (d / "node_modules").mkdir()
    (d / "node_modules" / "pkg.js").write_text("z")
    (d / ".DS_Store").write_text("junk")
    assert registry.list_files(d) == ["app.py", "sub/b.py"]
