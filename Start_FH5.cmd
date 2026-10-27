@echo off
setlocal
cd /d "%~dp0"
if exist "dist\FH5CrateRadar.exe" (
  start "" "dist\FH5CrateRadar.exe" %*
  exit /b 0
)
where pythonw.exe >nul 2>nul
if errorlevel 1 (
  echo Install 64-bit Python with Tk or build dist\FH5CrateRadar.exe first.
  pause
  exit /b 1
)
start "" pythonw.exe "%~dp0fh5_desktop.pyw" %*
endlocal
