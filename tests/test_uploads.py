import io
import zipfile
from pathlib import Path

import pytest

from app import uploads


def build_zip(path, entries: dict[str, str], absolute: str | None = None):
    """entries: {name_in_zip: content}. absolute adds a member with the given raw name."""
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
        if absolute:
            zf.writestr(absolute, "evil")
    return path


def test_extract_normal_zip_unwraps_single_folder(settings, tmp_path):
    z = build_zip(tmp_path / "t.zip", {"mytool/app.py": "print(1)", "mytool/tool.yml": "run: python app.py"})
    src = uploads.extract_zip(z, settings)
    assert src.name == "mytool"
    assert (src / "app.py").is_file()


def test_extract_flat_zip_returns_staging_root(settings, tmp_path):
    z = build_zip(tmp_path / "t.zip", {"app.py": "print(1)"})
    src = uploads.extract_zip(z, settings)
    assert (src / "app.py").is_file()
    assert src.parent.resolve() == settings.staging_dir.resolve()


def test_zip_slip_relative_traversal_rejected(settings, tmp_path):
    z = build_zip(tmp_path / "evil.zip", {"ok.txt": "x", "../../evil.txt": "pwned"})
    with pytest.raises(uploads.UploadError):
        uploads.extract_zip(z, settings)
    # nothing escaped
    assert not (settings.data_dir.parent / "evil.txt").exists()


def test_zip_absolute_path_rejected(settings, tmp_path):
    z = build_zip(tmp_path / "abs.zip", {"ok.txt": "x"}, absolute="/tmp/evil.txt")
    with pytest.raises(uploads.UploadError):
        uploads.extract_zip(z, settings)


def test_symlink_members_skipped(settings, tmp_path):
    z = tmp_path / "sym.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("app.py", "print(1)")
        info = zipfile.ZipInfo("link")
        info.external_attr = (0xA1FF) << 16  # symlink mode
        zf.writestr(info, "/etc/passwd")
    src = uploads.extract_zip(z, settings)
    assert (src / "app.py").is_file()
    assert not (src / "link").exists()


def test_not_a_zip_rejected(settings, tmp_path):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"this is not a zip")
    with pytest.raises(uploads.UploadError):
        uploads.extract_zip(bad, settings)


def test_install_collision_raises_409(settings, tmp_path):
    (settings.tools_dir / "taken").mkdir()
    src = settings.staging_dir / "newtool"
    src.mkdir()
    with pytest.raises(uploads.UploadError) as exc:
        uploads.install_tool_dir(src, "taken", settings)
    assert exc.value.status_code == 409


def test_install_replace(settings):
    (settings.tools_dir / "taken").mkdir()
    (settings.tools_dir / "taken" / "old.txt").write_text("old")
    src = settings.staging_dir / "newtool"
    src.mkdir()
    (src / "new.txt").write_text("new")
    dest = uploads.install_tool_dir(src, "taken", settings, replace=True)
    assert (dest / "new.txt").is_file()
    assert not (dest / "old.txt").exists()


def test_sanitize_tool_id():
    assert uploads.sanitize_tool_id("My Cool Tool.zip") == "My-Cool-Tool"
    assert uploads.sanitize_tool_id("whisper_v2") == "whisper_v2"
    with pytest.raises(uploads.UploadError):
        uploads.sanitize_tool_id("///")


def test_safe_join_rejects_traversal(tmp_path):
    with pytest.raises(uploads.UploadError):
        uploads.safe_join(tmp_path, "../evil.txt")
    with pytest.raises(uploads.UploadError):
        uploads.safe_join(tmp_path, "sub/../../evil.txt")


def test_safe_join_treats_leading_slash_as_relative(tmp_path):
    # a leading "/" (e.g. from a multipart filename) is normalized away rather
    # than treated as an absolute filesystem path — same as the old upload_folder
    # behavior this was extracted from
    dest = uploads.safe_join(tmp_path, "/etc/passwd")
    assert dest == (tmp_path / "etc" / "passwd").resolve()


def test_safe_join_rejects_empty(tmp_path):
    with pytest.raises(uploads.UploadError):
        uploads.safe_join(tmp_path, "")
    with pytest.raises(uploads.UploadError):
        uploads.safe_join(tmp_path, "///")


def test_safe_join_allows_nested_relative_path(tmp_path):
    dest = uploads.safe_join(tmp_path, "sub/dir/file.txt")
    assert dest == (tmp_path / "sub" / "dir" / "file.txt").resolve()


def test_write_read_delete_tool_file_roundtrip(tmp_path):
    uploads.write_tool_file(tmp_path, "src/app.py", "print(1)")
    assert uploads.read_tool_file(tmp_path, "src/app.py") == "print(1)"
    uploads.delete_tool_file(tmp_path, "src/app.py")
    with pytest.raises(uploads.UploadError) as exc:
        uploads.read_tool_file(tmp_path, "src/app.py")
    assert exc.value.status_code == 404


def test_write_tool_file_too_large_rejected(tmp_path):
    big = "x" * (uploads.MAX_FILE_BYTES + 1)
    with pytest.raises(uploads.UploadError) as exc:
        uploads.write_tool_file(tmp_path, "big.txt", big)
    assert exc.value.status_code == 413


def test_read_tool_file_missing_raises_404(tmp_path):
    with pytest.raises(uploads.UploadError) as exc:
        uploads.read_tool_file(tmp_path, "nope.txt")
    assert exc.value.status_code == 404


def test_create_tool_dir_scaffolds_nested_files(settings):
    dest = uploads.create_tool_dir("newtool", {"app.py": "print(1)", "sub/x.txt": "y"}, settings)
    assert (dest / "app.py").read_text() == "print(1)"
    assert (dest / "sub" / "x.txt").read_text() == "y"


def test_create_tool_dir_rejects_existing(settings):
    (settings.tools_dir / "taken").mkdir()
    with pytest.raises(uploads.UploadError) as exc:
        uploads.create_tool_dir("taken", {"a.py": "1"}, settings)
    assert exc.value.status_code == 409


def test_create_tool_dir_rejects_invalid_id(settings):
    with pytest.raises(uploads.UploadError):
        uploads.create_tool_dir("../evil", {"a.py": "1"}, settings)
    assert not (settings.tools_dir.parent / "evil").exists()


def test_create_tool_dir_cleans_up_on_bad_file_path(settings):
    with pytest.raises(uploads.UploadError):
        uploads.create_tool_dir("halfbaked", {"ok.py": "1", "../escape.py": "2"}, settings)
    assert not (settings.tools_dir / "halfbaked").exists()


def test_link_tool_dir_creates_symlink_to_source(settings, tmp_path):
    src = tmp_path / "my-dev-tool"
    src.mkdir()
    (src / "app.py").write_text("print(1)")

    dest = uploads.link_tool_dir("linked", src, settings)
    assert dest.is_symlink()
    assert dest.resolve() == src.resolve()
    assert (dest / "app.py").read_text() == "print(1)"

    # it's a live link — edits on the source side show up through the link
    (src / "app.py").write_text("print(2)")
    assert (dest / "app.py").read_text() == "print(2)"


def test_link_tool_dir_rejects_relative_path(settings, tmp_path):
    with pytest.raises(uploads.UploadError):
        uploads.link_tool_dir("linked", Path("relative/dir"), settings)


def test_link_tool_dir_rejects_missing_or_non_directory(settings, tmp_path):
    with pytest.raises(uploads.UploadError):
        uploads.link_tool_dir("linked", tmp_path / "nope", settings)
    f = tmp_path / "a_file.txt"
    f.write_text("x")
    with pytest.raises(uploads.UploadError):
        uploads.link_tool_dir("linked", f, settings)


def test_link_tool_dir_rejects_existing_tool_id(settings, tmp_path):
    (settings.tools_dir / "taken").mkdir()
    src = tmp_path / "src"
    src.mkdir()
    with pytest.raises(uploads.UploadError) as exc:
        uploads.link_tool_dir("taken", src, settings)
    assert exc.value.status_code == 409


def test_remove_tool_dir_unlinks_symlink_without_touching_target(settings, tmp_path):
    src = tmp_path / "precious-dev-folder"
    src.mkdir()
    (src / "app.py").write_text("print(1)")
    dest = uploads.link_tool_dir("linked", src, settings)

    uploads.remove_tool_dir(dest)

    assert not dest.exists() and not dest.is_symlink()
    assert src.is_dir()
    assert (src / "app.py").read_text() == "print(1)"


def test_remove_tool_dir_rmtrees_a_real_directory(settings):
    d = settings.tools_dir / "copied"
    d.mkdir()
    (d / "app.py").write_text("print(1)")
    uploads.remove_tool_dir(d)
    assert not d.exists()
