@echo off
echo ========================================
echo  Building Bitrate Guardian
echo ========================================
echo.

REM Install dependencies
echo Installing dependencies...
pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: Failed to install dependencies
    pause
    exit /b 1
)

echo.
echo Building executable...
pyinstaller build.spec --clean
if errorlevel 1 (
    echo ERROR: Build failed
    pause
    exit /b 1
)

echo.
echo ========================================
echo  Build complete!
echo  Output: dist\BitrateGuardian.exe
echo ========================================
pause
