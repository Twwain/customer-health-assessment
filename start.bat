@echo off
chcp 65001 >nul

:: Kill old processes that might hold the ports
taskkill /f /im python.exe 2>nul
taskkill /f /im node.exe 2>nul
timeout /t 2 /nobreak >nul

echo.
echo ===================================
echo   Customer Health Assessment
echo   backend: http://localhost:8000
echo   frontend: http://localhost:5173
echo   Close this window to stop all
echo ===================================
echo.

:: Start backend in a new window, with correct working directory
start "Backend" /d "%~dp0backend" cmd /k "python -m uvicorn main:app --port 8000"

:: Small delay to let backend start
timeout /t 3 /nobreak >nul

:: Start frontend in a new window, with correct working directory
start "Frontend" /d "%~dp0frontend" cmd /k "npx vite --host 0.0.0.0 --port 5173"

echo.
echo Waiting for servers to be ready...
timeout /t 5 /nobreak >nul

echo Opening browser...
start http://localhost:5173

echo.
echo System is running. Close backend and frontend windows to stop.
echo.
pause
