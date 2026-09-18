@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist venv\Scripts\python.exe (
  start "" venv\Scripts\pythonw.exe app.py
) else (
  start "" pythonw app.py
)
