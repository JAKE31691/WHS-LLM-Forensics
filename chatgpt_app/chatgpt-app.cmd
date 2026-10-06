@echo off
setlocal
set "APP_DIR=%~dp0"
if not exist "%APP_DIR%.venv\Scripts\python.exe" (
    >&2 echo Error: Python environment not found at "%APP_DIR%.venv".
    >&2 echo Run "python -m venv .venv" and install requirements.txt in the tool folder.
    exit /b 1
)
set "CHATGPT_APP_ENTRY=chatgpt-app.cmd"
"%APP_DIR%.venv\Scripts\python.exe" "%APP_DIR%main.py" %*
exit /b %ERRORLEVEL%
