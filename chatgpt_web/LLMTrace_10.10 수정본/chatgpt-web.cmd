@echo off
if "%~1"=="" goto :usage
if "%~2"=="" goto :usage
if not "%~3"=="" goto :usage
call "%~dp0_run-environment.cmd" chatgpt_web "%~1" "%~2"
exit /b %ERRORLEVEL%

:usage
echo Usage: .\chatgpt-web.cmd ^<E01 path or live^> ^<absolute output path^>
echo Example: .\chatgpt-web.cmd "C:\Cases\case.E01" "D:\LLMTrace\case-result"
echo Example: .\chatgpt-web.cmd live "D:\LLMTrace\live-result"
exit /b 2
