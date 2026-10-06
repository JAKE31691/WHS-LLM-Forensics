@echo off
if "%~1"=="" goto :usage
if "%~2"=="" goto :usage
if not "%~3"=="" goto :usage
call "%~dp0_run-environment.cmd" claude_app "%~1" "%~2"
exit /b %ERRORLEVEL%

:usage
echo Usage: .\claude-desktop.cmd ^<E01 path or live^> ^<absolute output path^>
echo Example: .\claude-desktop.cmd "C:\Cases\case.E01" "D:\LLMTrace\case-result"
echo Example: .\claude-desktop.cmd live "D:\LLMTrace\live-result"
exit /b 2
