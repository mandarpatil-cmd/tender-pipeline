@echo off
cd /d "%~dp0"

echo Downloading the latest code...
git pull origin main
if errorlevel 1 (
  echo.
  echo The download failed. Leave this window open and send a picture of it.
  pause
  exit /b 1
)

echo.
echo Installing libraries and updating the database...
uv sync --frozen
if errorlevel 1 (
  echo.
  echo Install failed. Leave this window open and send a picture of it.
  pause
  exit /b 1
)

uv run pipeline-db init
if errorlevel 1 (
  echo.
  echo The database update failed. Leave this window open and send a picture of it.
  pause
  exit /b 1
)

echo.
echo Update finished. Double-click start-ui.bat.
pause