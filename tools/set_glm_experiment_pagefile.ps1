#Requires -RunAsAdministrator
param(
    [Parameter(Mandatory)][string]$RunDirectory,
    [ValidateSet('Apply','Restore')][string]$Action = 'Apply'
)
$ErrorActionPreference = 'Stop'
$baseline = Get-Content -LiteralPath (Join-Path $RunDirectory 'A\system-before.json') -Raw | ConvertFrom-Json
$aState = Get-Content -LiteralPath (Join-Path $RunDirectory 'A\status.json') -Raw | ConvertFrom-Json
if ($aState.state -ne 'complete') { throw 'Phase A must complete before changing the page file.' }
$original = @($baseline.settings | Where-Object { $_.Name -ieq 'C:\pagefile.sys' })
if ($original.Count -ne 1 -or $baseline.computer.AutomaticManagedPagefile) {
    throw 'Unexpected baseline settings; stop for manual review.'
}
$setting = Get-CimInstance Win32_PageFileSetting | Where-Object { $_.Name -ieq 'C:\pagefile.sys' }
if (-not $setting) { throw 'The original C: page file setting is missing.' }
if ($Action -eq 'Apply') {
    $drive = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='C:'"
    if ($drive.FreeSpace -lt 40GB) { throw 'At least 40 GiB free on C: is required before this experiment.' }
    if ($setting.InitialSize -ne $original[0].InitialSize -or $setting.MaximumSize -ne $original[0].MaximumSize) {
        throw 'Page file settings changed since baseline; stop for review.'
    }
    $sizes = @{ InitialSize = [uint32]32768; MaximumSize = [uint32]32768 }
} else {
    $sizes = @{ InitialSize = [uint32]$original[0].InitialSize; MaximumSize = [uint32]$original[0].MaximumSize }
}
$before = $setting | Select-Object Name,InitialSize,MaximumSize
$before | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $RunDirectory ('pagefile-before-'+$Action+'.json')) -Encoding utf8
$setting | Set-CimInstance -Property $sizes | Out-Null
$after = Get-CimInstance Win32_PageFileSetting | Where-Object { $_.Name -ieq 'C:\pagefile.sys' }
if ($after.InitialSize -ne $sizes.InitialSize -or $after.MaximumSize -ne $sizes.MaximumSize) {
    throw 'Page file configuration verification failed.'
}
[ordered]@{
    action = $Action
    configured = $after | Select-Object Name,InitialSize,MaximumSize
    active = @(Get-CimInstance Win32_PageFileUsage | Select-Object Name,AllocatedBaseSize)
    updated = (Get-Date).ToString('o')
    note = 'Configuration changed; reboot may be required. No automatic restart was performed.'
} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $RunDirectory 'pagefile-change.json') -Encoding utf8
Write-Output 'Page file configuration verified. Save your work and restart Windows before phase B.'
