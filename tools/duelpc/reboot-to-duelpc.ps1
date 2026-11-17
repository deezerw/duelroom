# Windows side: restart ONCE into the duelpc Linux install (next boot only;
# the boot after that goes back to the normal default). Run it by
# right-clicking -> "Run with PowerShell"; it asks for admin rights.
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process powershell "-ExecutionPolicy Bypass -File `"$PSCommandPath`"" -Verb RunAs
    exit
}
# firmware boot entries are blocks separated by blank lines
$blocks = ((bcdedit /enum firmware) -join "`n") -split "`n\s*`n"
$entry = $blocks | Where-Object { $_ -match 'description\s+ubuntu' } | Select-Object -First 1
if (-not $entry) { Write-Host "No 'ubuntu' firmware boot entry found."; Read-Host "Enter to close"; exit 1 }
$id = [regex]::Match($entry, 'identifier\s+(\{[^}]+\})').Groups[1].Value
bcdedit /set "{fwbootmgr}" bootsequence $id | Out-Null
Write-Host "Restarting into duelpc (Ubuntu) in 10 s - Ctrl+C to cancel."
Start-Sleep 10
shutdown /r /t 0
