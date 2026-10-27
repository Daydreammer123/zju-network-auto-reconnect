param(
    [ValidateSet('enable', 'disable', 'status')]
    [string]$Action = 'enable'
)

$ErrorActionPreference = 'Stop'
$taskName = 'ZJU-Automatic-Reconnection'
$entryPath = Join-Path $PSScriptRoot 'startup_reconnection.ps1'
$shellPath = (Get-Process -Id $PID).Path
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
. (Join-Path $PSScriptRoot 'monitor_settings.ps1')

if ($existing) {
    $ours = @($existing.Actions | Where-Object {
        $_.Arguments -and $_.Arguments.IndexOf($entryPath, [StringComparison]::OrdinalIgnoreCase) -ge 0
    }).Count -gt 0
    if (-not $ours) { throw 'A task with the same name belongs to a different project. No changes were made.' }
}

if ($Action -eq 'status') {
    if (-not $existing) { 'Autostart is not configured.'; exit 1 }
    $info = Get-ScheduledTaskInfo -TaskName $taskName
    $existing | Select-Object TaskName, State, @{Name='Enabled'; Expression={$_.Settings.Enabled}},
        @{Name='User'; Expression={$_.Principal.UserId}}, @{Name='Trigger'; Expression={'Current user logon'}} | Format-List
    $info | Select-Object LastRunTime, LastTaskResult, NextRunTime | Format-List
    exit 0
}

if ($Action -eq 'disable') {
    if ($existing) {
        Disable-ScheduledTask -TaskName $taskName | Out-Null
        'Autostart disabled. Existing running monitors are not stopped.'
    } else { 'Autostart is not configured.' }
    exit 0
}

if (-not (Test-Path -LiteralPath $PSScriptRoot -PathType Container) -or
    -not (Test-Path -LiteralPath $entryPath -PathType Leaf)) {
    throw 'Startup entry point is missing.'
}

$pythonPath = (Get-Command python.exe -ErrorAction Stop).Source
$failover = Get-ZjuFailoverEnabled -ProjectDirectory $PSScriptRoot
$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -File "{0}" -PythonPath "{1}"' -f `
    $entryPath, $pythonPath
$taskAction = New-ScheduledTaskAction -Execute $shellPath -Argument $arguments -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $trigger -Principal $principal `
    -Settings $settings -Description 'Start Windows-native ZJU network recovery and UU remote watchdog.' `
    -Force | Out-Null
"Autostart enabled: $taskName"
"Trigger: logon of current user; native-only A/B failover=$failover."
"Python: $pythonPath"
