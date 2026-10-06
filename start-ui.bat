@echo off
cd /d "%~dp0"

echo Starting the tender pipeline window...
start "Tender pipeline" cmd /k "uv run ops-ui"

echo Waiting for the server...
timeout /t 4 /nobreak >nul

start "" "http://127.0.0.1:8000"
echo The browser should be open. Leave the "Tender pipeline" window open.
pause