param(
    [string]$DestinationRoot = 'D:\Models'
)

$ErrorActionPreference = 'Stop'
# This download must not consume the user's metered local proxy.  Clear inherited
# proxy variables and also tell curl explicitly to connect directly.
$env:HTTP_PROXY = $null
$env:HTTPS_PROXY = $null
$env:ALL_PROXY = $null
$env:NO_PROXY = '*'
# The official host is not directly reachable on this network.  hf-mirror only
# supplies the transport redirect; every completed file is checked against
# Project Maya's official SHA-256 before it is accepted.
$repoBase = 'https://hf-mirror.com/peasantsmith/GLM-5.3-Flash-Maya-GGUF/resolve/main'
$modelDir = Join-Path $DestinationRoot 'GLM-5.3-Flash\Maya-S24'
$visionDir = Join-Path $modelDir 'vision'
$statusPath = Join-Path $modelDir 'download-status.json'

$files = @(
    @{ Relative = 'GLM-5.3-Flash-Maya-S24-IQ2_XXS_S-00001-of-00003.gguf'; Remote = 'Maya-S24/GLM-5.3-Flash-Maya-S24-IQ2_XXS_S-00001-of-00003.gguf'; Bytes = 46886573024; Sha256 = '8ebd5c74bcf65ece2a8699b7b6fd2b8d177f350ed252cee252fdd99b86a0138c' },
    @{ Relative = 'GLM-5.3-Flash-Maya-S24-IQ2_XXS_S-00002-of-00003.gguf'; Remote = 'Maya-S24/GLM-5.3-Flash-Maya-S24-IQ2_XXS_S-00002-of-00003.gguf'; Bytes = 46982144736; Sha256 = '4bd445da3a0128a9c5b32228c924e0a622aa4a132c7a8dc9a2c207a4beea8e34' },
    @{ Relative = 'GLM-5.3-Flash-Maya-S24-IQ2_XXS_S-00003-of-00003.gguf'; Remote = 'Maya-S24/GLM-5.3-Flash-Maya-S24-IQ2_XXS_S-00003-of-00003.gguf'; Bytes = 832958464; Sha256 = 'a6fd4e88007ac49b431c7d02be34e43ab7a09224d53a0c52782327613cb41917' },
    @{ Relative = 'vision\mmproj-GLM-5.3-Flash-F16.gguf'; Remote = 'vision/mmproj-GLM-5.3-Flash-F16.gguf'; Bytes = 1128046944; Sha256 = '3627575df16bd152db0f3fd7e488d270b33f3a9e6c7fa3b1b8ac381faafde882' },
    @{ Relative = 'vision\GLM-5.3-Flash-vocab.gguf'; Remote = 'vision/GLM-5.3-Flash-vocab.gguf'; Bytes = 9426880; Sha256 = '8f53cb1bd2e631c14ef413e3284735d9e53f3c508d07a6f609e705b487105912' }
)

function Write-Status {
    param([string]$State, [string]$File = '', [string]$Message = '')
    [ordered]@{
        state = $State
        file = $File
        message = $Message
        updated = (Get-Date).ToString('o')
        process_id = $PID
        destination = $modelDir
    } | ConvertTo-Json | Set-Content -LiteralPath $statusPath -Encoding utf8
}

function Test-ExpectedFile {
    param([string]$Path, [long]$Bytes, [string]$Sha256)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    if ((Get-Item -LiteralPath $Path).Length -ne $Bytes) { return $false }
    # Use the .NET implementation directly so background verification works even
    # when Windows PowerShell cannot autoload Microsoft.PowerShell.Utility.
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $hasher = [System.Security.Cryptography.SHA256]::Create()
        try {
            $hashBytes = $hasher.ComputeHash($stream)
            $actualHash = ([System.BitConverter]::ToString($hashBytes)).Replace('-', '').ToLowerInvariant()
        }
        finally {
            $hasher.Dispose()
        }
    }
    finally {
        $stream.Dispose()
    }
    return $actualHash -eq $Sha256
}

New-Item -ItemType Directory -Force -Path $modelDir, $visionDir | Out-Null
Write-Status -State 'running' -Message 'Preparing downloads'

try {
    $downloads = @()
    foreach ($entry in $files) {
        $finalPath = Join-Path $modelDir $entry.Relative
        $partialPath = "$finalPath.part"
        $url = "$repoBase/$($entry.Remote)"
        Write-Status -State 'running' -File $entry.Relative -Message 'Downloading or verifying'

        if (Test-ExpectedFile -Path $finalPath -Bytes $entry.Bytes -Sha256 $entry.Sha256) {
            Write-Output "Already verified: $finalPath"
            continue
        }
        if (Test-Path -LiteralPath $finalPath) {
            throw "Existing file does not match the expected size or SHA-256: $finalPath"
        }

        if (Test-Path -LiteralPath $partialPath -PathType Leaf) {
            $partialLength = (Get-Item -LiteralPath $partialPath).Length
            if ($partialLength -gt $entry.Bytes) {
                throw "Partial file is larger than expected: $partialPath"
            }
            if ($partialLength -eq $entry.Bytes) {
                if (-not (Test-ExpectedFile -Path $partialPath -Bytes $entry.Bytes -Sha256 $entry.Sha256)) {
                    throw "Completed partial file failed SHA-256 verification: $partialPath"
                }
                Move-Item -LiteralPath $partialPath -Destination $finalPath
                Write-Output "Verified: $finalPath"
                continue
            }
        }

        $curlLog = "$partialPath.curl.log"
        $arguments = @(
            '--noproxy', '*', '--location', '--fail', '--show-error', '--continue-at', '-',
            '--retry', '20', '--retry-all-errors', '--retry-delay', '10', '--connect-timeout', '30',
            '--output', $partialPath, $url
        )
        $process = Start-Process -FilePath 'curl.exe' -ArgumentList $arguments -WindowStyle Hidden `
            -RedirectStandardError $curlLog -PassThru
        $downloads += [pscustomobject]@{
            Entry = $entry
            FinalPath = $finalPath
            PartialPath = $partialPath
            CurlLog = $curlLog
            Process = $process
        }
        Write-Output "Started direct download PID $($process.Id): $($entry.Relative)"
    }

    Write-Status -State 'running' -Message "Downloading $($downloads.Count) files in parallel without a proxy"
    foreach ($download in $downloads) {
        $download.Process.WaitForExit()
        # Start-Process can leave the cached Process object without ExitCode after a
        # long-running child exits. Refresh it explicitly before reading the code.
        $download.Process.Refresh()
        $exitCode = $download.Process.ExitCode
        if ($null -eq $exitCode -or $exitCode -ne 0) {
            throw "curl exited with code $exitCode while downloading $($download.Entry.Relative); see $($download.CurlLog)"
        }
    }
    foreach ($download in $downloads) {
        Write-Status -State 'verifying' -File $download.Entry.Relative -Message 'Checking size and SHA-256'
        if (-not (Test-ExpectedFile -Path $download.PartialPath -Bytes $download.Entry.Bytes -Sha256 $download.Entry.Sha256)) {
            throw "Downloaded file failed size or SHA-256 verification: $($download.PartialPath)"
        }
        Move-Item -LiteralPath $download.PartialPath -Destination $download.FinalPath
        Write-Output "Verified: $($download.FinalPath)"
    }
    Write-Status -State 'complete' -Message 'All model and vision files passed SHA-256 verification'
    Write-Output 'All downloads completed and verified.'
}
catch {
    Write-Status -State 'failed' -Message $_.Exception.Message
    Write-Error $_
    exit 1
}
