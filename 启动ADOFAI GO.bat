@echo off
rem ADOFAI GO launcher.
rem Double-click: opens the chart library page.
rem With arguments: passed through to run.cmd unchanged.
rem Always goes through run.cmd, which picks the repo .venv itself. The old
rem pythonw entry was removed: if PATH Python lacks PySide6, pythonw exits
rem silently (no console) and no window ever appears.
setlocal
cd /d "%~dp0"

if not exist "%~dp0run.cmd" goto no_runner

if "%~1"=="" (
    call "%~dp0run.cmd" --page library --tab local
) else (
    call "%~dp0run.cmd" %*
)
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
    echo.
    echo ADOFAI GO failed to start. Exit code: %CODE%
    echo See the message above for details.
    pause
)
exit /b %CODE%

:no_runner
echo ERROR: run.cmd was not found next to this file.
echo Re-extract ADOFAI GO and try again.
pause
exit /b 1
