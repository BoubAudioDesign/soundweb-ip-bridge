@echo off
rem Lance la passerelle au premier plan (double-clic ou invite de commandes). Ctrl+C pour arreter.
rem Arguments supplementaires transmis tels quels, ex. : start.bat -v   /   start.bat --simulate
cd /d "%~dp0..\.."
if not exist ".venv\Scripts\python.exe" (
    echo Environnement absent : lancez d'abord deploy\windows\install.ps1
    pause
    exit /b 1
)
if not exist "config.yaml" (
    echo config.yaml absent : copiez config.example.yaml en config.yaml et adaptez-le.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m soundweb_bridge -c config.yaml %*
pause
