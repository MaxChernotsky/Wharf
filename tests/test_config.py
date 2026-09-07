from app import auth
from app.config import get_settings, load_overrides, save_overrides


def test_save_and_load_overrides_roundtrip(tmp_path):
    assert load_overrides(tmp_path) == {}
    save_overrides(tmp_path, tools_port_range="9000-9010")
    assert load_overrides(tmp_path) == {"tools_port_range": "9000-9010"}


def test_save_overrides_ignores_unknown_fields(tmp_path):
    save_overrides(tmp_path, tools_port_range="9000-9010", dashboard_port="1234")
    assert load_overrides(tmp_path) == {"tools_port_range": "9000-9010"}


def test_save_overrides_merges_with_existing(tmp_path):
    save_overrides(tmp_path, tools_port_range="9000-9010")
    save_overrides(tmp_path, tools_port_range="9100-9110")
    assert load_overrides(tmp_path) == {"tools_port_range": "9100-9110"}


def test_get_settings_bootstraps_password_hash_from_env_once(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AUTH_PASSWORD", "bootstrap-password")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.auth_password_hash
        assert auth.verify_password("bootstrap-password", settings.auth_password_hash)
        assert load_overrides(settings.state_dir)["auth_password_hash"] == settings.auth_password_hash

        # a later change via the settings UI must not be clobbered by the
        # still-set env var on the next boot
        save_overrides(settings.state_dir, auth_password_hash="changed-by-ui")
        get_settings.cache_clear()
        settings = get_settings()
        assert settings.auth_password_hash == "changed-by-ui"
    finally:
        get_settings.cache_clear()
