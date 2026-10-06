@echo off
cd /d "%~dp0"
uv sync --frozen
uv run pipeline-db init
echo Setup finished. Fill in the .env files, then double-click start-ui.bat.
pause