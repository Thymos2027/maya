param(
    [Parameter(Mandatory)][string]$RunDirectory,
    [ValidateSet('A','B')][string]$Phase = 'A'
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$runPath = [IO.Path]::GetFullPath($RunDirectory)
if ($runPath.Contains('"')) { throw 'Unexpected quote in run path.' }
$phasePath = Join-Path $runPath $Phase
New-Item -ItemType Directory -Path $phasePath -Force | Out-Null
if (Test-Path -LiteralPath (Join-Path $phasePath 'status.json')) {
    throw 'This phase already has a status file; inspect it before rerunning.'
}
$python = Join-Path $repo '.venv\Scripts\python.exe'
$runner = Join-Path $PSScriptRoot 'compare_glm_commit.py'
$arguments = @('-u', ('"'+$runner+'"'), '--out', ('"'+$runPath+'"'), '--phase', $Phase)
$process = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $repo -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $phasePath 'stdout.log') `
    -RedirectStandardError (Join-Path $phasePath 'stderr.log') -PassThru
$process.Id | Set-Content -LiteralPath (Join-Path $phasePath 'launcher.pid')
Write-Output "Phase $Phase launcher PID: $($process.Id)"
Write-Output "Status and logs: $phasePath"
