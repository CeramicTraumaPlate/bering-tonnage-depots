@echo off
rem Start the Auto-Transfer Depots overlay in a console window that shows what it is doing.
rem (Close the console window to quit the overlay. The window stays open if it crashes, so you can read why.)
cd /d "%~dp0"
start "Depots Overlay" cmd /k python -m atd.overlay %*
