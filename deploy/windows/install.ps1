# Installation de la passerelle Soundweb sous Windows (environnement Python + configuration).
# Usage (depuis n'importe où) :  powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Root

# Python : lanceur "py" de préférence, sinon "python"
$py = Get-Command py -ErrorAction SilentlyContinue
if ($py) { $PyExe = "py"; $PyArgs = @("-3") } else { $PyExe = "python"; $PyArgs = @() }
& $PyExe @PyArgs -c "import sys; assert sys.version_info >= (3, 8), sys.version"
if ($LASTEXITCODE -ne 0) { throw "Python 3.8 ou plus récent est requis." }

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Création de l'environnement virtuel .venv ..."
    & $PyExe @PyArgs -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Échec de la création du venv." }
}
$Venv = Join-Path $Root ".venv\Scripts\python.exe"
Write-Host "Installation des dépendances ..."
& $Venv -m pip install --disable-pip-version-check -q -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Échec de pip install." }

if (-not (Test-Path "config.yaml")) {
    Copy-Item config.example.yaml config.yaml
    Write-Host "config.yaml créé à partir de config.example.yaml."
}

Write-Host ""
Write-Host "Ports série détectés :"
& $Venv -m soundweb_bridge --list-ports
Write-Host ""
Write-Host "Étapes suivantes :"
Write-Host "  1. notepad config.yaml   (serial.port: COMx, handles, sources)"
Write-Host "  2. Test sans matériel :  .\.venv\Scripts\python.exe -m soundweb_bridge -c config.yaml --simulate"
Write-Host "  3. Lancement réel     :  deploy\windows\start.bat"
Write-Host "  4. Démarrage auto     :  deploy\windows\install-service.ps1 (en administrateur)"
