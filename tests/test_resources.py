from axion_wizard.utils.resources import (
    read_template_bytes,
    read_template_text,
    template_filesystem_path,
)


def test_read_template_text_returns_content() -> None:
    text = read_template_text("env.j2")
    assert "POSTGRES_PASSWORD" in text


def test_read_template_bytes_matches_text_encoding() -> None:
    data = read_template_bytes("env.j2")
    assert data.decode("utf-8") == read_template_text("env.j2")


def test_template_filesystem_path_gives_real_path_with_content() -> None:
    with template_filesystem_path("fastapi/main.py") as path:
        assert path.exists()
        assert "FastAPI" in path.read_text(encoding="utf-8")
