@echo off
REM ActTrace - Claude Desktop chat artifact recovery
REM Portable launcher. Finds the package relative to this file, so the
REM whole folder can be copied to a USB stick or another analyst PC.
REM Keep this file ASCII-only: cmd.exe parses it in the active codepage.
setlocal

REM 1) explicit override
if defined ACTRACE_HOME goto :check

REM 2) package sitting next to this file  (USB layout: 2-analysis\ActTrace\)
if exist "%~dp0ActTrace\actrace_main.py" (
    set "ACTRACE_HOME=%~dp0ActTrace"
    goto :check
)

REM 3) this file living inside the package folder
if exist "%~dp0actrace_main.py" (
    set "ACTRACE_HOME=%~dp0."
    goto :check
)

REM 4) installed location on the analyst workstation
if exist "C:\Forensics\tools\ActTrace\actrace_main.py" (
    set "ACTRACE_HOME=C:\Forensics\tools\ActTrace"
    goto :check
)

echo [ActTrace] package not found.
echo             Put ActTrace\ next to this file, or set ACTRACE_HOME.
exit /b 9

:check
if not exist "%ACTRACE_HOME%\actrace_main.py" (
    echo [ActTrace] bad ACTRACE_HOME: %ACTRACE_HOME%
    exit /b 9
)

where python >nul 2>&1
if errorlevel 1 (
    echo [ActTrace] Python not found on PATH.
    echo             ActTrace runs on the ANALYST machine, not the target PC.
    exit /b 9
)

set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

python -u "%ACTRACE_HOME%\actrace_main.py" %*
exit /b %ERRORLEVEL%
