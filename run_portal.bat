@echo off
setlocal
title Conflict Check Portal
rem ------------------------------------------------------------------------------
rem  One-click starter. Double-click this file.
rem  It finds a suitable Python (3.10 or newer), then runs bootstrap.py, which
rem  builds/repairs everything (virtual environment, packages, folders, database)
rem  and starts the portal. The window stays open so you can read the log.
rem  Close the window (or press Ctrl+C) to stop the portal.
rem  Extra options are passed through, e.g.:  run_portal.bat --port 8600
rem ------------------------------------------------------------------------------
pushd "%~dp0"

set "PY="
for %%C in ("py -3.13" "py -3.12" "py -3.11" "py -3.10" "py -3" "python") do (
    if not defined PY (
        %%~C -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)" >nul 2>nul
        if not errorlevel 1 set "PY=%%~C"
    )
)
if not defined PY goto :nopython

echo.
echo  Using: %PY%
echo  Folder: %CD%
echo.
%PY% bootstrap.py %*
set "RC=%ERRORLEVEL%"
goto :done

:nopython
echo.
echo  ERROR: Python 3.10 or newer was not found on this computer.
echo  Install it from https://www.python.org/downloads/ and tick
echo  "Add python.exe to PATH" on the first installer screen, then run this file again.
set "RC=9009"

:done
echo.
if "%RC%"=="0" (
    echo  The portal has stopped.
) else (
    echo  Stopped with error code %RC%. Read the messages above, or open data\logs\bootstrap.log
)
echo.
popd
pause
endlocal & exit /b %RC%
