@echo off
setlocal
set SYS=%SystemRoot%\System32
cd /d "%~dp0"
"%SYS%\fltmc.exe" >nul 2>&1
if errorlevel 1 (
  echo Please right-click install.bat and choose "Run as administrator".
  pause & exit /b 1
)
"%SYS%\where.exe" python >nul 2>&1
if errorlevel 1 (
  echo Python was not found. Install Python 3 from python.org and tick "Add python.exe to PATH", then run this again.
  pause & exit /b 1
)
python -m pip install --upgrade psutil
if errorlevel 1 ( echo pip install failed & pause & exit /b 1 )

set DEST=%ProgramData%\NasScreenAgent
mkdir "%DEST%" >nul 2>&1
copy /y nasscreen_agent.py "%DEST%\" >nul
if exist agent.json copy /y agent.json "%DEST%\" >nul

echo.
python "%DEST%\nasscreen_agent.py" --setup

for /f "delims=" %%i in ('python -c "import sys,os;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))"') do set PYW=%%i
"%SYS%\schtasks.exe" /Create /TN "NasScreenAgent" /TR "\"%PYW%\" \"%DEST%\nasscreen_agent.py\"" /SC ONSTART /RU SYSTEM /RL HIGHEST /F
"%SYS%\schtasks.exe" /End /TN "NasScreenAgent" >nul 2>&1
"%SYS%\schtasks.exe" /Run /TN "NasScreenAgent"
echo.
echo Done. The agent starts with Windows (before login) and reports to the NAS every second.
echo Log: %DEST%\agent.log
pause
