@echo off
cd /d "%~dp0"
echo Starting IG Tag Parser UI...
if exist "venv\Scripts\python.exe" (
  "venv\Scripts\python.exe" app.py
) else if exist "..\venv\Scripts\python.exe" (
  "..\venv\Scripts\python.exe" app.py
) else (
  py -3 app.py
)
