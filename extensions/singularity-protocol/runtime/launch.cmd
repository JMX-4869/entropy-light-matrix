@echo off
setlocal enabledelayedexpansion

rem Singularity Protocol - OGAS console launcher
rem
rem NOTE: this file must stay PURE ASCII. cmd.exe parses .cmd files using the
rem system code page (GBK on Chinese Windows) BEFORE chcp takes effect, so any
rem UTF-8 non-ASCII bytes here get mangled and fragments run as commands.
rem
rem NOTE 2: no multi-line `for %%P in (...) do (...)` blocks here. In practice
rem the multi-line list form misbehaved (the follow-up `if not defined` still
rem fired even though the variable was set). Plain sequential `if` calls are
rem ugly but deterministic.

set "HERE=%~dp0"
set "OGAS="

call :try_home
if defined OGAS goto have_ogas

if defined SMD_OGAS_HOME call :try_dir "%SMD_OGAS_HOME%"
if defined OGAS goto have_ogas

call :try_dir "E:\yiuxi\OGAS"
if defined OGAS goto have_ogas
call :try_dir "%USERPROFILE%\OGAS"
if defined OGAS goto have_ogas
call :try_dir "%USERPROFILE%\Documents\OGAS"
if defined OGAS goto have_ogas
call :try_dir "C:\OGAS"
if defined OGAS goto have_ogas
call :try_dir "%HERE%..\..\..\..\OGAS"
if defined OGAS goto have_ogas

echo.
echo   [Singularity Protocol] OGAS not found.
echo.
echo   OGAS is a separate GPL-3.0 project. Download it first:
echo     https://github.com/Nostalgia-and-Imperial-Auto-Governor/OGAS
echo.
echo   Then do EITHER of these:
echo     - write its folder path into: %HERE%ogas_home.txt  (one line, path only)
echo     - or set environment variable SMD_OGAS_HOME
echo.
pause
exit /b 2

:have_ogas

set "PY="
if exist "%OGAS%\.venv\Scripts\python.exe" set "PY=%OGAS%\.venv\Scripts\python.exe"
if not defined PY (
    where py >nul 2>nul && set "PY=py"
)
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo.
    echo   [Singularity Protocol] Python not found.
    echo   OGAS needs Python 3.11+. Install it, then relaunch this extension.
    echo.
    pause
    exit /b 3
)

set "CFG=%HERE%agent_config.json"
if not exist "%CFG%" set "CFG=%OGAS%\apps\control_center\agent_config.windows.example.json"

echo.
echo   [Singularity Protocol] OGAS:    %OGAS%
echo   [Singularity Protocol] Python:  %PY%
echo   [Singularity Protocol] Console: http://127.0.0.1:8765
echo   [Singularity Protocol] Close this window to stop.
echo.

rem web_console.py imports `apps.control_center...` and `iag...`, so the project
rem root must be on sys.path. Running it by file path puts sys.path[0] at
rem apps\control_center instead -- hence -m plus PYTHONPATH.
set "PYTHONPATH=%OGAS%"
cd /d "%OGAS%"
"%PY%" -m apps.control_center.web_console --config "%CFG%" --host 127.0.0.1 --port 8765

echo.
echo   [Singularity Protocol] console exited.
pause
exit /b 0

:try_home
if not exist "%HERE%ogas_home.txt" exit /b 0
set "LINE="
set /p LINE=<"%HERE%ogas_home.txt%"
if defined LINE call :try_dir "%LINE%"
exit /b 0

:try_dir
if defined OGAS exit /b 0
if not exist "%~1\apps\control_center\web_console.py" exit /b 0
set "OGAS=%~1"
exit /b 0
