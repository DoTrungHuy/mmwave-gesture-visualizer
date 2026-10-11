@echo off
chcp 65001 >nul
title IWR6843AOP 网页版点云
echo ============================================
echo   IWR6843AOP 网页版实时点云
echo   启动后用浏览器打开  http://127.0.0.1:8080
echo   停止：直接关闭本窗口（或按 Ctrl+C）
echo   注意：先关掉桌面版查看器和 TI 可视化网页，串口是独占的
echo ============================================
set "PYTHONIOENCODING=utf-8"
if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" "%~dp0web_viewer.py" %*
) else if exist "%~dp0..\.venv\Scripts\python.exe" (
  "%~dp0..\.venv\Scripts\python.exe" "%~dp0web_viewer.py" %*
) else (
  python "%~dp0web_viewer.py" %*
)
pause
