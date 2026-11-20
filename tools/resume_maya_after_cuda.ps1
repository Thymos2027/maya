param(
    [int]$InstallerProcessId = 32668
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$modelDir = 'D:\Models\Maya-S24'
$cudaRoot = 'C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.8'
$nvcc = Join-Path $cudaRoot 'bin\nvcc.exe'
$statusPath = Join-Path $modelDir 'cuda-maya-bootstrap-status.json'
$outLog = Join-Path $modelDir 'maya-setup-cuda128.out.log'
$errLog = Join-Path $modelDir 'maya-setup-cuda128.err.log'

function Write-BootstrapStatus {
    param([string]$State, [string]$Message, [int]$ProcessId = 0)
    [ordered]@{
        state = $State
        message = $Message
        updated = (Get-Date).ToString('o')
        process_id = $ProcessId
        cuda_root = $cudaRoot
        output_log = $outLog
        error_log = $errLog
    } | ConvertTo-Json | Set-Content -LiteralPath $statusPath -Encoding utf8
}

try {
    $installerStarted = (Get-Process -Id $InstallerProcessId -ErrorAction SilentlyContinue).StartTime
    if (-not $installerStarted) { $installerStarted = Get-Date }
    while (Get-Process -Id $InstallerProcessId -ErrorAction SilentlyContinue) {
        $elapsed = (Get-Date) - $installerStarted
        $elapsedText = '{0:00}:{1:00}:{2:00}' -f [int]$elapsed.TotalHours, $elapsed.Minutes, $elapsed.Seconds
        Write-BootstrapStatus -State 'installing_cuda' `
            -Message "CUDA 12.8 installer is active; elapsed $elapsedText" -ProcessId $InstallerProcessId
        Start-Sleep -Seconds 10
    }

    $required = @(
        $nvcc,
        (Join-Path $cudaRoot 'include\cuda_runtime.h'),
        (Join-Path $cudaRoot 'include\cublas_v2.h'),
        (Join-Path $cudaRoot 'lib\x64\cublas.lib'),
        (Join-Path $cudaRoot 'lib\x64\cublasLt.lib')
    )
    $missing = @($required | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) })
    if ($missing.Count -gt 0) {
        throw "CUDA installation ended without required files: $($missing -join ', ')"
    }

    $env:CUDA_PATH = $cudaRoot
    $env:CUDA_PATH_V12_8 = $cudaRoot
    $env:Path = "$cudaRoot\bin;$cudaRoot\lib\x64;$env:Path"
    $env:HTTP_PROXY = 'http://127.0.0.1:7890'
    $env:HTTPS_PROXY = 'http://127.0.0.1:7890'
    $env:ALL_PROXY = 'http://127.0.0.1:7890'
    $env:NO_PROXY = '127.0.0.1,localhost'

    Write-BootstrapStatus -State 'building_maya' -Message 'CUDA verified; rebuilding Maya for RTX 5060 Ti (sm_120)'
    $python = Join-Path $repo '.venv\Scripts\python.exe'
    $mayaArgs = @(
        'maya.py', '--setup', '--model', 'glm-maya-s24', '--gguf-dir', $modelDir,
        '--gpu', '0', '--context', '32768', '--parallel', '1', '--vram-reserve-mib', '1200',
        '--yes', '--plain', '--rebuild', '--nvcc', ('"' + $nvcc + '"')
    )
    $maya = Start-Process -FilePath $python -ArgumentList $mayaArgs -WorkingDirectory $repo `
        -WindowStyle Hidden -RedirectStandardOutput $outLog -RedirectStandardError $errLog -PassThru

    $buildStarted = Get-Date
    while (-not $maya.HasExited) {
        $listener = Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue
        if ($listener) {
            Write-BootstrapStatus -State 'running' -Message 'Maya is listening on http://127.0.0.1:8080' -ProcessId $listener.OwningProcess
            exit 0
        }
        $elapsed = (Get-Date) - $buildStarted
        $elapsedText = '{0:00}:{1:00}:{2:00}' -f [int]$elapsed.TotalHours, $elapsed.Minutes, $elapsed.Seconds
        Write-BootstrapStatus -State 'building_maya' `
            -Message "Maya setup/build process is active; elapsed $elapsedText" -ProcessId $maya.Id
        Start-Sleep -Seconds 10
        $maya.Refresh()
    }

    throw "Maya setup exited with code $($maya.ExitCode); see $outLog and $errLog"
}
catch {
    Write-BootstrapStatus -State 'failed' -Message $_.Exception.Message
    Write-Error $_
    exit 1
}
