@echo off
chcp 65001 >nul
net session >nul 2>nul
if errorlevel 1 (
  echo 正在请求管理员权限，请在弹出的 UAC 窗口中点击"是"...
  powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~f0'"
  exit /b 0
)
title 局域网文字投递工具（管理员）
cd /d "%~dp0"
set PYTHON_CMD=python
where python >nul 2>nul || set PYTHON_CMD=py
where %PYTHON_CMD% >nul 2>nul
if errorlevel 1 (
  echo 未找到 Python，请先安装 Python 并勾选加入 PATH
  pause
  exit /b 1
)
echo 正在启动局域网文字投递工具，请勿关闭本窗口...
%PYTHON_CMD% shoujishuru.py
echo.
echo 程序已退出，按任意键关闭窗口...
pause >nul