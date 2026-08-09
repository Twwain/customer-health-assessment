@echo off
chcp 65001 >nul

::: 按端口清理残留进程：只杀监听 8000 / 5173 的 PID，避免误伤其他 python / node
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /c:":8000 " ^| findstr "LISTENING"') do taskkill /f /pid %%p 2>nul
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /c:":5173 " ^| findstr "LISTENING"') do taskkill /f /pid %%p 2>nul
timeout /t 2 /nobreak >nul

echo.
echo ===================================
echo   Customer Health Assessment
echo   backend: http://localhost:8000
echo   frontend: http://localhost:5173
echo   Close this window to stop all
echo ===================================
echo.

::: Start backend in a new window, with correct working directory
::: Prefer project venv python (bare "python" may resolve to an old system Python without deps)
set "PY=python"
set "SECRET_KEY_FILE=%~dp0.ch_secret"
if exist "%~dp0backend\.venv\Scripts\python.exe" set "PY=%~dp0backend\.venv\Scripts\python.exe"
start "Backend" /d "%~dp0backend" cmd /k ""%PY%" -m uvicorn main:app --port 8000"

::: Small delay to let backend start
timeout /t 3 /nobreak >nul

::: Start frontend in a new window, with correct working directory
::: 直接用 node 运行 vite（不依赖 npm/npx，避免 PATH 与沙箱权限差异）
start "Frontend" /d "%~dp0frontend" cmd /k "node node_modules\vite\bin\vite.js --host 0.0.0.0 --port 5173"

echo.
echo Waiting for servers to be ready...
timeout /t 5 /nobreak >nul

echo Opening browser...
start http://localhost:5173

echo.
echo System is running. Close backend and frontend windows to stop.
echo.
pause
