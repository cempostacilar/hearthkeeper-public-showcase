@echo off
cd /d "%~dp0"
echo Creating database...
python db_setup.py
if %ERRORLEVEL% EQU 0 (
    echo.
    echo Done. You should now see autostock.db in this folder.
) else (
    echo.
    echo Python may not be in your PATH. Try running "python db_setup.py" from Command Prompt in this folder.
)
pause
