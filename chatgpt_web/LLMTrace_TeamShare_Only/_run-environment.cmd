@echo off
setlocal EnableExtensions
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"

set "LLMTRACE_PROFILE=%~1"
set "LLMTRACE_SOURCE=%~2"
set "LLMTRACE_OUTPUT=%~3"

set "LLMTRACE_PY=%LLMTRACE_PYTHON%"
if not defined LLMTRACE_PY if exist "%~dp0.venv\Scripts\python.exe" set "LLMTRACE_PY=%~dp0.venv\Scripts\python.exe"
if not defined LLMTRACE_PY if exist "%~dp0..\..\ccl-env-0318\Scripts\python.exe" set "LLMTRACE_PY=%~dp0..\..\ccl-env-0318\Scripts\python.exe"
if not defined LLMTRACE_PY set "LLMTRACE_PY=python"

pushd "%~dp0" >nul
"%LLMTRACE_PY%" -m llmtrace.environment_cli --profile "%LLMTRACE_PROFILE%" "%LLMTRACE_SOURCE%" "%LLMTRACE_OUTPUT%"
set "LLMTRACE_EXIT=%ERRORLEVEL%"
popd >nul

if not "%LLMTRACE_EXIT%"=="0" echo [LLMTrace] analysis failed with exit code %LLMTRACE_EXIT%.
exit /b %LLMTRACE_EXIT%
