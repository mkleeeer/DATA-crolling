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
start "Image Crawler Server" cmd /k ""%PYTHON_EXE%" app.py"
ping -n 3 127.0.0.1 >nul
start "" http://127.0.0.1:5000
