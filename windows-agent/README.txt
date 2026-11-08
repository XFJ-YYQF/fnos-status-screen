NAS Screen - Windows agent
==========================
1. Install Python 3 (python.org, tick "Add python.exe to PATH").
2. Unzip first (do not run from inside the zip). Right-click install.bat -> Run as administrator. Enter the NAS IP when asked.
   (Token: only if you set "agent_token" in the NAS app's config.json.)
3. Windows on  -> NAS screen shows this PC.   Windows off/asleep -> back to the NAS overview (about 8 s later).

Optional - CPU/GPU temperatures, fans, CPU power:
  Run LibreHardwareMonitor as administrator, Options -> Remote Web Server -> Run (port 8085).
  The agent picks it up automatically. Without it CPU temperature is not available on Windows.

Manual test:  python nasscreen_agent.py --once      (prints the JSON that would be sent)
Logs:         C:\ProgramData\NasScreenAgent\agent.log
Remove:       uninstall.bat (as administrator)
