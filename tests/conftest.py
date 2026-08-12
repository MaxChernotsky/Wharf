import pytest

from app.config import Settings


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings(
        data_dir=tmp_path,
        tools_dir=tmp_path / "tools",
        tools_port_range="18100-18110",
        stop_grace_seconds=2.0,
    )
    s.ensure_dirs()
    return s
