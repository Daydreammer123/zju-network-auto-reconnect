param(
    [ValidateSet('enable', 'disable', 'status')]
    [string]$Action = 'status',
    [string]$RuntimeArchive,
    [string]$UserSid
)

$ErrorActionPreference = 'Stop'
$taskName = 'ZJU-Automatic-Reconnection-Boot'
$root = Join-Path $env:ProgramData 'ZJU-Automatic-Reconnection'
$manifestPath = Join-Path $root 'installation.json'
$expectedHash = '9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38'
# Official release: https://www.python.org/downloads/release/python-31317/
# Private runtime only; Anaconda and Windows Wi-Fi profiles are never modified.

function Assert-NoReparse([string]$Path) {
    $current = [IO.Path]::GetFullPath($Path)
    while ($current) {
        if (Test-Path -LiteralPath $current) {
            if ((Get-Item -LiteralPath $current -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw 'Deployment path contains a reparse point. No deployment is allowed.'
            }
        }
        $parent = Split-Path -Parent $current
        if ($parent -eq $current) { break }
        $current = $parent
    }
}

function Set-ProtectedAcl([string]$Path, [bool]$Directory, [string]$ReaderSid, [bool]$Writable = $false) {
    $admins = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
    $system = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    if ($Directory) { $acl = [Security.AccessControl.DirectorySecurity]::new() }
    else { $acl = [Security.AccessControl.FileSecurity]::new() }
    $acl.SetOwner($admins)
    $acl.SetAccessRuleProtection($true, $false)
    $inherit = [Security.AccessControl.InheritanceFlags]::None
    if ($Directory) { $inherit = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit' }
    foreach ($sid in @($admins, $system)) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl', $inherit, 'None', 'Allow'))
    }
    if ($ReaderSid) {
        $rights = [Security.AccessControl.FileSystemRights]::ReadAndExecute
        if ($Writable) { $rights = [Security.AccessControl.FileSystemRights]'Read, Write, Synchronize' }
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            [Security.Principal.SecurityIdentifier]::new($ReaderSid), $rights, $inherit, 'None', 'Allow'))
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing -and @($existing.Actions | Where-Object {
    $_.Execute -and $_.Execute.StartsWith("$root\releases\", [StringComparison]::OrdinalIgnoreCase) -and
    $_.Arguments -match '--boot-network'
}).Count -eq 0) { throw 'Task name is owned by a different installation. No changes were made.' }

if ($Action -eq 'status') {
    if (-not $existing) {
        if (Test-Path -LiteralPath $manifestPath) {
            'A protected installation exists, but task details are not readable. Run status in elevated PowerShell.'
            exit 2
        }
        'Boot recovery is not installed.'; exit 1
    }
    $existing | Select-Object TaskName, State, @{n='Enabled';e={$_.Settings.Enabled}}, @{n='Account';e={$_.Principal.UserId}} | Format-List
    Get-ScheduledTaskInfo -TaskName $taskName | Select-Object LastRunTime, LastTaskResult | Format-List
    "Protected installation: $root"
    exit 0
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Administrator approval is required. Run this script in an elevated PowerShell.'
}
if ($Action -eq 'disable') {
    if ($existing) {
        Disable-ScheduledTask -TaskName $taskName | Out-Null
        Stop-ScheduledTask -TaskName $taskName
    }
    'Boot recovery disabled and stopped. Wi-Fi and UU service were not changed.'
    exit 0
}

if (-not $RuntimeArchive -or -not (Test-Path -LiteralPath $RuntimeArchive -PathType Leaf)) {
    throw 'Supply the official Python 3.13.17 embeddable amd64 ZIP using -RuntimeArchive.'
}
if (-not $UserSid) { $UserSid = $identity.User.Value }
$null = [Security.Principal.SecurityIdentifier]::new($UserSid)
Assert-NoReparse $root
if (Test-Path -LiteralPath $root) {
    $owner = (Get-Acl -LiteralPath $root).GetOwner([Security.Principal.SecurityIdentifier]).Value
    if ($owner -notin @('S-1-5-18', 'S-1-5-32-544') -or -not (Test-Path -LiteralPath $manifestPath)) {
        throw 'Existing deployment directory is not a protected installation. Refusing to overwrite it.'
    }
    Assert-NoReparse $manifestPath
    if ((Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json).product -ne 'ZJU-Automatic-Reconnection') {
        throw 'Existing deployment marker belongs to a different product.'
    }
} else { $null = New-Item -ItemType Directory -Path $root }
Set-ProtectedAcl $root $true $UserSid
if (-not (Test-Path -LiteralPath $manifestPath)) {
    @{product='ZJU-Automatic-Reconnection';status='preparing';source_project=$PSScriptRoot} |
        ConvertTo-Json | Set-Content -LiteralPath $manifestPath -Encoding UTF8
}

$release = Join-Path $root ('releases\' + (Get-Date -Format 'yyyyMMdd-HHmmss-ffff'))
$runtime = Join-Path $release 'runtime'
$app = Join-Path $release 'app'
$config = Join-Path $root 'config'
$logs = Join-Path $root 'logs'
$state = Join-Path $root 'state'
foreach ($directory in @($runtime, $app, $config, $logs, $state)) {
    $null = New-Item -ItemType Directory -Path $directory -Force
    Assert-NoReparse $directory
}
# Remove all ordinary-user access BEFORE copying any credential-bearing files.
Set-ProtectedAcl $config $true ''
$archive = Join-Path $release 'runtime.zip'
Copy-Item -LiteralPath $RuntimeArchive -Destination $archive
if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedHash) {
    throw 'Official runtime checksum mismatch. Nothing will be executed or registered.'
}
Expand-Archive -LiteralPath $archive -DestinationPath $runtime
$signature = Get-AuthenticodeSignature -LiteralPath (Join-Path $runtime 'python.exe')
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
    throw 'Python executable signature validation failed.'
}
[IO.File]::WriteAllText((Join-Path $runtime 'python313._pth'), "python313.zip`r`n.`r`n..\app`r`n", [Text.Encoding]::ASCII)
$files = @('host_reconnect.py','wifi_control.py','wifi_failover.py','network_checks.py',
    'srun_client.py','uu_remote_monitor.py','runtime_control.py')
foreach ($file in $files) {
    $source = Join-Path $PSScriptRoot $file
    Assert-NoReparse $source
    Copy-Item -LiteralPath $source -Destination (Join-Path $app $file)
}
$envSource = Join-Path $PSScriptRoot '.env'
Assert-NoReparse $envSource
$envPath = Join-Path $config 'boot.env'
Copy-Item -LiteralPath $envSource -Destination (Join-Path $config 'boot.env.new')
Move-Item -LiteralPath (Join-Path $config 'boot.env.new') -Destination $envPath -Force
$lockPath = Join-Path $state 'network.lock'
if (-not (Test-Path -LiteralPath $lockPath)) { [IO.File]::WriteAllBytes($lockPath, [byte[]]@(0)) }
Assert-NoReparse $lockPath
# The user may lock this byte, but cannot delete it, change its ACL or write code/config.
Set-ProtectedAcl $lockPath $false $UserSid $true
$python = Join-Path $runtime 'python.exe'
$entry = Join-Path $app 'host_reconnect.py'
$logPath = Join-Path $logs 'boot_monitor.log'

# Preflight is LOCAL ONLY: no authentication, external request, connection or GUI launch.
& $python -I -X utf8 $entry --boot-network --diagnose --env-file $envPath
if ($LASTEXITCODE -ne 0) { throw 'Protected runtime local preflight failed; task was not registered.' }
$record = [ordered]@{product='ZJU-Automatic-Reconnection'; release=$release; runtime_version='3.13.17';
    archive_sha256=$expectedHash; source_project=$PSScriptRoot; user_sid=$UserSid;
    files=@($files | ForEach-Object { @{name=$_;sha256=(Get-FileHash -LiteralPath (Join-Path $app $_) -Algorithm SHA256).Hash} })}
$record | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $manifestPath -Encoding UTF8
$arguments = '-I -u -X utf8 "{0}" --boot-network --env-file "{1}" --log-file "{2}"' -f $entry, $envPath, $logPath
$taskAction = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $runtime
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId 'S-1-5-18' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
if ($existing) { Stop-ScheduledTask -TaskName $taskName }
Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $trigger -Principal $principal `
    -Settings $settings -Description 'Protected pre-logon Wi-Fi/authentication recovery and official UU service watchdog.' -Force | Out-Null
# Permit diagnostics from the owner user's filtered token without allowing task
# execution, modification, deletion or permission changes. Keep existing SYSTEM/admin ACEs.
$scheduler = New-Object -ComObject 'Schedule.Service'
$scheduler.Connect()
$registered = $scheduler.GetFolder('\').GetTask($taskName)
$descriptor = [Security.AccessControl.RawSecurityDescriptor]::new($registered.GetSecurityDescriptor(4))
$readAce = [Security.AccessControl.CommonAce]::new([Security.AccessControl.AceFlags]::None,
    [Security.AccessControl.AceQualifier]::AccessAllowed, 0x120089,
    [Security.Principal.SecurityIdentifier]::new($UserSid), $false, $null)
$descriptor.DiscretionaryAcl.InsertAce($descriptor.DiscretionaryAcl.Count, $readAce)
$registered.SetSecurityDescriptor($descriptor.GetSddlForm([Security.AccessControl.AccessControlSections]::Access), 0)
Start-ScheduledTask -TaskName $taskName
"Installed and started: $taskName"
"Log: $logPath"
'No Wi-Fi profile, auto-logon, proxy or UU configuration was modified.'
