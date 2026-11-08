@echo off
cd /d "%~dp0"
python stardew_sync.py sync %*
pause
