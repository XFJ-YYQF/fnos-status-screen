@echo off
set SYS=%SystemRoot%\System32
"%SYS%\fltmc.exe" >nul 2>&1
if errorlevel 1 ( echo Run as administrator. & pause & exit /b 1 )
"%SYS%\schtasks.exe" /End /TN "NasScreenAgent" >nul 2>&1
"%SYS%\schtasks.exe" /Delete /TN "NasScreenAgent" /F
rmdir /s /q "%ProgramData%\NasScreenAgent"
echo Removed.
pause
