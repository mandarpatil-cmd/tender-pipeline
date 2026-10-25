"""update.bat is what the client double-clicks. Its order is the safety net."""

from __future__ import annotations

from pipeline_core.settings import project_root

#: Byte for byte what every shipped update.bat has up to the pull. A running copy
#: carries on at this offset in the file the pull writes, so it must never move.
FROZEN_PREFIX = (
    '@echo off\ncd /d "%~dp0"\n\necho Downloading the latest code...\ngit pull origin main\n'
)


def _script() -> str:
    raw = (project_root() / "update.bat").read_bytes().decode("ascii")
    return raw.replace("\r\n", "\n")


def test_the_lines_up_to_the_pull_never_change():
    assert _script().startswith(FROZEN_PREFIX)


def test_the_database_is_copied_before_it_is_updated():
    lines = [line.strip() for line in _script().splitlines()]
    backup = lines.index("uv run pipeline-db backup --label update")
    init = lines.index("uv run pipeline-db init")
    assert lines.index("uv sync --frozen") < backup < init


def test_an_update_never_drops_contacts_by_itself():
    assert "recheck-contacts" not in _script()
