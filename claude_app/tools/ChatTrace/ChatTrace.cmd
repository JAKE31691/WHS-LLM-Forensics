@echo off
REM ChatTrace - Claude Desktop chat artifact recovery
REM Portable launcher. Finds the package relative to this file, so the
REM whole folder can be copied to a USB stick or another analyst PC.
REM Keep this file ASCII-only: cmd.exe parses it in the active codepage.
setlocal

REM 1) explicit override
if defined CHATTRACE_HOME goto :check

REM 2) package sitting next to this file  (USB layout: 2-analysis\ChatTrace\)
if exist "%~dp0ChatTrace\chattrace_main.py" (
    set "CHATTRACE_HOME=%~dp0ChatTrace"
    goto :check
)

REM 3) this file living inside the package folder
if exist "%~dp0chattrace_main.py" (
    set "CHATTRACE_HOME=%~dp0."
    goto :check
)

REM 4) installed location on the analyst workstation
if exist "C:\Forensics\tools\ChatTrace\chattrace_main.py" (
    set "CHATTRACE_HOME=C:\Forensics\tools\ChatTrace"
    goto :check
)

echo [ChatTrace] package not found.
echo             Put ChatTrace\ next to this file, or set CHATTRACE_HOME.
exit /b 9

:check
if not exist "%CHATTRACE_HOME%\chattrace_main.py" (
    echo [ChatTrace] bad CHATTRACE_HOME: %CHATTRACE_HOME%
    exit /b 9
)

where python >nul 2>&1
if errorlevel 1 (
    echo [ChatTrace] Python not found on PATH.
    echo             ChatTrace runs on the ANALYST machine, not the target PC.
    exit /b 9
)

set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

python -u "%CHATTRACE_HOME%\chattrace_main.py" %*
exit /b %ERRORLEVEL%
