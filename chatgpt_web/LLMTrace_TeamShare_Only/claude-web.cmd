@echo off
if "%~1"=="" goto :usage
if "%~2"=="" goto :usage
if not "%~3"=="" goto :usage
call "%~dp0_run-environment.cmd" claude_web "%~1" "%~2"
exit /b %ERRORLEVEL%

:usage
echo Usage: .\claude-web.cmd ^<E01 path or live^> ^<absolute output path^>
echo Note: the claude_web profile is not implemented yet.
exit /b 2
