# Supprime le démarrage automatique et la règle de pare-feu (PowerShell ADMINISTRATEUR).
param([string]$TaskName = "SoundwebBridge")
$ErrorActionPreference = "Stop"
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Tâche '$TaskName' supprimée."
} else {
    Write-Host "Tâche '$TaskName' absente."
}
Get-NetFirewallRule -DisplayName "Soundweb Bridge" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
Write-Host "Règle de pare-feu supprimée."
