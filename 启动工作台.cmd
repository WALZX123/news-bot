@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0workbench\start.ps1" -Action Open
if errorlevel 1 pause
