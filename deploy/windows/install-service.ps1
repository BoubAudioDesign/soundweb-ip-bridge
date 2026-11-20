# Démarrage automatique de la passerelle au boot de Windows (équivalent du service systemd Linux).
# Crée une tâche planifiée "SoundwebBridge" (compte SYSTEM, au démarrage, redémarrage auto en cas d'arrêt)
# et ouvre les ports HTTP/TCP dans le pare-feu.
# Usage, dans un PowerShell ADMINISTRATEUR :
#   powershell -ExecutionPolicy Bypass -File deploy\windows\install-service.ps1 [-Config C:\chemin\config.yaml] [-Hex]
param(
    [string]$Config = "",
    [string]$TaskName = "SoundwebBridge",
    [switch]$Hex          # journalise chaque trame série en hexadécimal (-v)
)
$ErrorActionPreference = "Stop"

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw "Ce script doit être lancé dans un PowerShell administrateur." }

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { throw "Environnement absent : lancez d'abord deploy\windows\install.ps1" }
if (-not $Config) { $Config = Join-Path $Root "config.yaml" }
$Config = (Resolve-Path $Config).Path
$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force $LogDir | Out-Null

# Lecture des ports dans la config (pour le pare-feu)
$ports = & $Python -c "import sys; from soundweb_bridge.config import load_config; c = load_config(sys.argv[1]); print(c.http_port, c.tcp_port)" $Config
if ($LASTEXITCODE -ne 0) { throw "config.yaml invalide : corrigez-le avant d'installer le service." }
$HttpPort, $TcpPort = $ports.Trim() -split "\s+"

$argList = "-m soundweb_bridge -c `"$Config`" --log-file `"$LogDir\bridge.log`""
if ($Hex) { $argList += " -v" }

$action = New-ScheduledTaskAction -Execute $Python -Argument $argList -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description "Passerelle BSS Soundweb 9088 RS-232 <-> IP" | Out-Null

$rule = "Soundweb Bridge"
Get-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $rule -Direction Inbound -Protocol TCP -LocalPort $HttpPort, $TcpPort -Action Allow | Out-Null

Start-ScheduledTask -TaskName $TaskName
Write-Host "Tâche '$TaskName' installée et démarrée (ports $HttpPort/HTTP et $TcpPort/TCP ouverts)."
Write-Host "Logs     : $LogDir\bridge.log   (Get-Content -Wait `"$LogDir\bridge.log`")"
Write-Host "Arrêt    : Stop-ScheduledTask $TaskName    Démarrage : Start-ScheduledTask $TaskName"
Write-Host "Après une modification de config.yaml : Stop-ScheduledTask $TaskName; Start-ScheduledTask $TaskName"
