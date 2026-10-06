@echo off
cd /d "%~dp0"
uv sync --all-packages --frozen
uv run --all-packages pipeline-db init
echo Setup finished. Fill in the .env files, then double-click start-ui.bat.
pause