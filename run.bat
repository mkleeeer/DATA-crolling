@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  set "PYTHON_EXE=.venv\Scripts\python.exe"
) else if exist "venv\Scripts\python.exe" (
  set "PYTHON_EXE=venv\Scripts\python.exe"
) else (
  echo Python virtual environment not found. Create one with: python -m venv .venv
  pause
  exit /b 1
)
"%PYTHON_EXE%" launch.py %*
if errorlevel 1 pause
