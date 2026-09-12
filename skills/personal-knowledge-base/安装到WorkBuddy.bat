@echo off
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Please ask your host Agent to prepare Python 3 with normal authorization.
  exit /b 2
)
py -3 -B scripts\install_workbuddy.py --apply %*
set "result=%errorlevel%"
pause
exit /b %result%
