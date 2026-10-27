param(
    [Parameter(Mandatory = $true)]
    [string]$PythonPath
)

$ErrorActionPreference = 'Stop'
$logPath = Join-Path $PSScriptRoot 'startup_reconnection.log'
$managerPath = Join-Path $PSScriptRoot 'native_monitor.ps1'
$shellPath = (Get-Process -Id $PID).Path
. (Join-Path $PSScriptRoot 'monitor_settings.ps1')

function Log-Startup([string]$Message) {
    $line = '[{0}] {1}{2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message, [Environment]::NewLine
    [System.IO.File]::AppendAllText($logPath, $line, [System.Text.UTF8Encoding]::new($false))
}

try {
    # Validate the mode without printing configuration values or credentials.
    $null = Get-ZjuFailoverEnabled -ProjectDirectory $PSScriptRoot
    Log-Startup 'Starting Windows-native Wi-Fi/authentication monitor.'
    Log-Startup 'Native-only startup: entering persistent supervisor; existing worker is reused.'
    & $shellPath -NoProfile -NonInteractive -File $managerPath supervise -PythonPath $PythonPath | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Native monitor could not be started.' }
    Log-Startup 'Native supervisor exited normally (another supervisor may already own this project).'
    exit 0
} catch {
    Log-Startup 'Startup failed. Check Python path, project access and native_monitor.error.log.'
    exit 1
}
