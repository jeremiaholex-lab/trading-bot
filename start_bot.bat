@echo off
title AI Trading Bot (Autonomous Market-Hours Engine)
color 0A
echo ========================================================
echo     Starting Autonomous AI Trading Bot...
echo     The bot will auto-trade during market hours
echo     (Mon-Fri 9:30 AM - 4:00 PM ET) and sleep otherwise.
echo     Press Ctrl+C at any time to stop safely.
echo ========================================================
echo.
cd /d "%~dp0"
call venv\Scripts\activate.bat
python bot.py --run --interval 60
pause
