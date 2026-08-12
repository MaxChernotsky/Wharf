from app.config import load_overrides, save_overrides


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
