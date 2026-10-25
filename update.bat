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
rem Never edit the lines above. cmd reads this file a line at a time, so a copy
rem that is running while the pull replaces it carries on at the same place in
rem the new file. The lines up to the pull must stay byte for byte the same.

echo.
echo Installing libraries...
uv sync --frozen
if errorlevel 1 (
  echo.
  echo Install failed. Leave this window open and send a picture of it.
  pause
  exit /b 1
)

echo.
echo Saving a copy of the database before it is updated...
uv run pipeline-db backup --label update
if errorlevel 1 (
  echo.
  echo The copy failed, so the database was not touched. Leave this window open and send a picture of it.
  pause
  exit /b 1
)

echo.
echo Updating the database...
uv run pipeline-db init
if errorlevel 1 (
  echo.
  echo The database update failed. Leave this window open and send a picture of it.
  pause
  exit /b 1
)

echo.
echo Update finished. Double-click start-ui.bat.
echo The copy from before this update is in data\backups.
pause
