@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "PYTHONUTF8=1"
chcp 65001 >nul
pushd "%~dp0" || exit /b 1
if exist ".venv\Scripts\python.exe" goto :dependencies

where py >nul 2>nul
if errorlevel 1 goto :python_on_path
py -3 -c "import sys; sys.exit(sys.version_info[:2] < (3, 9))" >nul 2>nul
if errorlevel 1 goto :python_on_path
py -3 -m venv ".venv"
if errorlevel 1 goto :failed
goto :dependencies

:python_on_path
python -c "import sys; sys.exit(sys.version_info[:2] < (3, 9))" >nul 2>nul
if errorlevel 1 goto :need_python
python -m venv ".venv"
if errorlevel 1 goto :failed

:dependencies
".venv\Scripts\python.exe" -c "import lxml.html, markdown_it, pypdf" >nul 2>nul
if not errorlevel 1 goto :launch
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :failed

:launch
".venv\Scripts\python.exe" launch.py %*
if errorlevel 1 goto :failed
popd
exit /b 0

:need_python
echo DraftPair needs Python 3.9 or newer. Install Python from https://www.python.org/downloads/windows/
echo Enable the Python launcher or add python.exe to PATH, then open this file again.
:failed
if not defined CI if not defined DRAFTPAIR_NONINTERACTIVE pause
popd
exit /b 1
