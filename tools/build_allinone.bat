@echo off
chcp 65001 >nul
python "%~dp0build_allinone.py" %*
if errorlevel 1 (
    echo.
    echo FAILED
    pause
    exit /b 1
)
echo.
pause
