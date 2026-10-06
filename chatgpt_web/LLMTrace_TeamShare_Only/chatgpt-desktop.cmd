@echo off
if "%~1"=="" goto :usage
if "%~2"=="" goto :usage
if not "%~3"=="" goto :usage
call "%~dp0_run-environment.cmd" chatgpt_desktop "%~1" "%~2"
exit /b %ERRORLEVEL%

:usage
echo Usage: .\chatgpt-desktop.cmd ^<E01 path or live^> ^<absolute output path^>
echo Note: the chatgpt_desktop profile is not implemented yet.
exit /b 2
