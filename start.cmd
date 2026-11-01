@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONPATH=%~dp0pylibs
set PYTHONIOENCODING=utf-8
echo ============================================
echo   keepsake - local album archiver
echo ============================================
echo.
echo  Starting local server, the browser will open...
echo  Keep this window open. Close it to stop.
echo.
python "%~dp0scripts\server.py"
echo.
echo Server stopped.
pause
