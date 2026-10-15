@echo off
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
rem Reuse a complete forensic environment, including an existing native tool
rem environment. A bare Python 3.12 interpreter is not sufficient for E01.
for %%P in (
  "%~dp0.venv\Scripts\python.exe"
  "%~dp0..\.venv\Scripts\python.exe"
  "%~dp0..\chatgpt_app\.venv\Scripts\python.exe"
  "%~dp0..\..\.venv\Scripts\python.exe"
) do (
  if exist "%%~fP" (
    "%%~fP" -c "import sys, pyewf, pytsk3, Registry; assert sys.version_info[:2] == (3, 12) and sys.maxsize > 2**32" >nul 2>nul
    if not errorlevel 1 (
      set "PYTHON_EXE=%%~fP"
      goto :run
    )
  )
)
py -3.12 -c "import sys, pyewf, pytsk3, Registry; assert sys.maxsize > 2**32" >nul 2>nul
if not errorlevel 1 goto :run_py
>&2 echo Error: No Python 3.12 x64 environment with pyewf, pytsk3, and Registry was found.
>&2 echo Create common\.venv and install common\requirements.txt, or use an existing compatible virtual environment.
exit /b 1

:run
"%PYTHON_EXE%" "%~dp0main.py" %*
exit /b %errorlevel%

:run_py
py -3.12 "%~dp0main.py" %*
exit /b %errorlevel%
