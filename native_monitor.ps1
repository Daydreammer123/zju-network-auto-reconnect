param(
    [ValidateSet('start', 'stop', 'status', 'supervise')]
    [string]$Action = 'start',
    [string]$PythonPath
)

$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path $PSScriptRoot 'host_reconnect.py'
$logPath = Join-Path $PSScriptRoot 'native_monitor.log'
$errorLogPath = Join-Path $PSScriptRoot 'native_monitor.error.log'

if (-not (Test-Path -LiteralPath $PSScriptRoot -PathType Container) -or
    -not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
    throw 'Native monitor project directory or entry point is missing.'
}

function Get-MonitorWorkers {
    @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -in @('python.exe', 'pythonw.exe') -and $_.CommandLine -and
        $_.CommandLine.IndexOf($scriptPath, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
        $_.CommandLine -notmatch '--(?:diagnose|once|boot-network)(?:\s|$)'
    })
}

$startupPath = Join-Path $PSScriptRoot 'startup_reconnection.ps1'
$supervisors = @(Get-CimInstance Win32_Process | Where-Object {
    $_.ProcessId -ne $PID -and $_.Name -in @('powershell.exe', 'pwsh.exe') -and $_.CommandLine -and
    (($_.CommandLine.IndexOf($PSCommandPath, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
        $_.CommandLine -match '\bsupervise\b') -or
     $_.CommandLine.IndexOf($startupPath, [StringComparison]::OrdinalIgnoreCase) -ge 0) -and
    $_.CommandLine -match '(?i)-File\s'
})
$workers = @(Get-MonitorWorkers)

if ($Action -eq 'stop') {
    $task = Get-ScheduledTask -TaskName 'ZJU-Automatic-Reconnection' -ErrorAction SilentlyContinue
    if ($task -and @($task.Actions | Where-Object {
        $_.Arguments -and $_.Arguments.IndexOf($startupPath, [StringComparison]::OrdinalIgnoreCase) -ge 0
    }).Count) { Stop-ScheduledTask -TaskName $task.TaskName }
    # Stop the supervisors first, otherwise they immediately respawn a stopped worker.
    foreach ($supervisor in $supervisors) {
        Stop-Process -Id $supervisor.ProcessId -ErrorAction SilentlyContinue
    }
    $workers = @(Get-MonitorWorkers)
    foreach ($worker in $workers) {
        Stop-Process -Id $worker.ProcessId -ErrorAction SilentlyContinue
        "Native monitor stopped: PID=$($worker.ProcessId)"
    }
    if ($workers.Count -eq 0) { 'Native monitor is not running.' }
    exit 0
}

if ($Action -eq 'status') {
    if ($workers.Count -gt 0) {
        foreach ($worker in $workers) { "Native monitor running: PID=$($worker.ProcessId)" }
        "Supervisor processes: $($supervisors.Count)"
        "Log: $logPath"
        exit 0
    }
    'Native monitor is not running.'
    exit 1
}

if ($PythonPath) {
    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) { throw 'Configured Python executable is missing.' }
    $python = $PythonPath
} else {
    $python = (Get-Command python.exe -ErrorAction Stop).Source
}
if ($Action -eq 'start') {
    if ($supervisors.Count -eq 0) {
        $task = Get-ScheduledTask -TaskName 'ZJU-Automatic-Reconnection' -ErrorAction SilentlyContinue
        $ours = $task -and $task.Settings.Enabled -and @($task.Actions | Where-Object {
            $_.Arguments -and $_.Arguments.IndexOf($startupPath, [StringComparison]::OrdinalIgnoreCase) -ge 0
        }).Count
        if ($ours) {
            Start-ScheduledTask -TaskName $task.TaskName
        } else {
            $shellPath = (Get-Process -Id $PID).Path
            $arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -File "{0}" supervise -PythonPath "{1}"' -f $PSCommandPath, $python
            $null = Start-Process -FilePath $shellPath -ArgumentList $arguments -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru
        }
    }
    Start-Sleep -Seconds 3
    $workers = @(Get-MonitorWorkers)
    if ($workers.Count -eq 0) { throw "Native monitor has not started. Check $errorLogPath" }
    foreach ($worker in $workers) { "Native monitor running with supervisor: PID=$($worker.ProcessId)" }
    "Log: $logPath"
    exit 0
}

# The logon task waits here for the lifetime of the worker. The mutex prevents
# manual 'start' and logon from creating competing supervisors in this session.
$hash = [Security.Cryptography.SHA256]::Create()
try { $id = [BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($scriptPath.ToLowerInvariant()))).Replace('-', '') }
finally { $hash.Dispose() }
$mutex = [Threading.Mutex]::new($false, "Local\ZjuMonitorSupervisor-$id")
$owned = $false
try {
    try { $owned = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $owned = $true }
    if (-not $owned) { 'Native monitor supervisor already running.'; exit 0 }
    while ($true) {
        $workers = @(Get-MonitorWorkers)
        if ($workers.Count -gt 0) {
            $worker = Get-Process -Id $workers[0].ProcessId -ErrorAction SilentlyContinue
        } else {
            foreach ($path in @($logPath, $errorLogPath)) {
                if (Test-Path -LiteralPath $path) { Copy-Item -LiteralPath $path -Destination "$path.previous" -Force }
            }
            $arguments = '-u -X utf8 "{0}"' -f $scriptPath
            $worker = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $PSScriptRoot `
                -WindowStyle Hidden -RedirectStandardOutput $logPath -RedirectStandardError $errorLogPath -PassThru
        }
        if ($worker) { $worker.WaitForExit() }
        # A clean or abnormal child exit is retried, without requiring a new logon.
        Start-Sleep -Seconds 10
    }
} finally {
    if ($owned) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
