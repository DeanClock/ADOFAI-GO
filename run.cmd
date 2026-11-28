@echo off
rem ADOFAI GO launcher.
rem Usage:  run.cmd [--page NAME] [--tab N] ...
rem Debug:  run.cmd --print-command [args]  prints the command line and exits.
setlocal
cd /d "%~dp0"

set "ROOT=%CD%"
set "PY="
set "REST="
set "PRINT_ONLY="

rem --- Collect arguments, dropping --print-command wherever it appears. ---
:collect
if "%~1"=="" goto collected
if /i "%~1"=="--print-command" goto mark_print
if defined REST (set "REST=%REST% %~1") else (set "REST=%~1")
goto next_arg
:mark_print
set "PRINT_ONLY=1"
:next_arg
shift
goto collect
:collected

rem 父进程带下来的旧 Python 环境变量会让 venv 启动器解析到错误的解释器
rem （典型报错：No Python at '"...python.exe'）。
set "__PYVENV_LAUNCHER__="
set "PYTHONHOME="

rem --- 1) Prefer a .venv that imports PySide6. ---
rem 仓库根的 .venv 优先；在 worktree 里运行时退到仓库根的 .venv。
set "VENV_DIR="
if exist "%ROOT%\.venv\Scripts\python.exe" set "VENV_DIR=%ROOT%\.venv"
if not defined VENV_DIR if exist "%ROOT%\..\..\..\.venv\Scripts\python.exe" (
    for %%i in ("%ROOT%\..\..\..\.venv") do set "VENV_DIR=%%~fi"
)
if defined VENV_DIR (
    "%VENV_DIR%\Scripts\python.exe" -c "import PySide6" >nul 2>nul
    if not errorlevel 1 set "PY=%VENV_DIR%\Scripts\python.exe"
)
if defined PY goto have_python

rem --- 2) Fall back to the base interpreter named in the venv's pyvenv.cfg. ---
set "VENV_HOME="
if defined VENV_DIR if exist "%VENV_DIR%\pyvenv.cfg" for /f "usebackq tokens=1,* delims== " %%a in ("%VENV_DIR%\pyvenv.cfg") do if /i "%%a"=="home" set "VENV_HOME=%%b"
if not defined VENV_HOME goto no_python
rem 配置里可能带引号（"C:\...\python.exe），一并去掉。
set "VENV_HOME=%VENV_HOME:"=%"
if not exist "%VENV_HOME%\python.exe" goto no_python
if defined PYTHONPATH goto merge_pythonpath
set "PYTHONPATH=%VENV_DIR%\Lib\site-packages;%ROOT%"
goto check_home_python
:merge_pythonpath
set "PYTHONPATH=%VENV_DIR%\Lib\site-packages;%ROOT%;%PYTHONPATH%"
:check_home_python
"%VENV_HOME%\python.exe" -c "import PySide6" >nul 2>nul
if errorlevel 1 goto no_python
set "PY=%VENV_HOME%\python.exe"

:have_python
if defined PRINT_ONLY goto print_command
"%PY%" "%ROOT%\run.py" %*
exit /b %ERRORLEVEL%

:print_command
echo "%PY%" "%ROOT%\run.py" %REST%
exit /b 0

:no_python
echo ERROR: No usable Python interpreter with PySide6 was found.
echo.
echo Checked these candidates:
echo   "%ROOT%\.venv\Scripts\python.exe"
echo   python.exe named by home = ... in "%ROOT%\.venv\pyvenv.cfg"
echo.
echo Fix: recreate the virtual environment and install dependencies:
echo   python -m venv .venv
echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
exit /b 1
