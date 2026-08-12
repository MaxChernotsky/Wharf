import zipfile

from app.export import build_export_zip


def make_tool_tree(settings):
    d = settings.tools_dir / "mytool"
    (d / ".venv" / "lib").mkdir(parents=True)
    (d / "node_modules" / "pkg").mkdir(parents=True)
    (d / "__pycache__").mkdir()
    (d / ".git").mkdir()
    (d / "src").mkdir()
    (d / "app.py").write_text("print(1)")
    (d / "tool.yml").write_text("run: python app.py")
    (d / "src" / "util.py").write_text("x = 1")
    (d / ".venv" / "lib" / "big.so").write_text("binary")
    (d / "node_modules" / "pkg" / "index.js").write_text("js")
    (d / "__pycache__" / "app.cpython-312.pyc").write_text("pyc")
    (d / ".git" / "HEAD").write_text("ref: refs/heads/main")
    return d


def test_export_excludes_heavy_dirs(settings):
    d = make_tool_tree(settings)
    zip_path = build_export_zip(d, settings.staging_dir)
    names = set(zipfile.ZipFile(zip_path).namelist())
    assert "mytool/app.py" in names
    assert "mytool/tool.yml" in names
    assert "mytool/src/util.py" in names
    assert not any(".venv" in n or "node_modules" in n or "__pycache__" in n for n in names)
    assert not any(".git" in n for n in names)


def test_export_can_include_git(settings):
    d = make_tool_tree(settings)
    zip_path = build_export_zip(d, settings.staging_dir, include_git=True)
    names = set(zipfile.ZipFile(zip_path).namelist())
    assert "mytool/.git/HEAD" in names
    assert not any(".venv" in n for n in names)
