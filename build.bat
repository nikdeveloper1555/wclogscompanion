@echo off
REM Build WCLogsEyeCompanion.exe -> dist\WCLogsEyeCompanion.exe
if not exist hub_defaults.json copy hub_defaults.example.json hub_defaults.json >nul
pip install -r requirements.txt
pyinstaller --onefile --windowed --name WCLogsEyeCompanion --icon icon.ico ^
  --add-data "hub_defaults.json;." --add-data "icon.ico;." --add-data "success.wav;." ^
  --hidden-import pystray._win32 --collect-all customtkinter companion.py
echo.
echo Done. Output: dist\WCLogsEyeCompanion.exe
