@echo off
rem Start the Auto-Transfer Depots overlay (no console window).
cd /d "%~dp0"
start "" pythonw -m atd.overlay %*
