# Shared raw .env lookup. Do not print raw lines: this file also contains credentials.
function Get-ZjuFailoverEnabled {
    param([string]$ProjectDirectory)
    $ErrorActionPreference = 'Stop'
    $value = [Environment]::GetEnvironmentVariable('ZJU_WIFI_FAILOVER', 'Process')
    if ($null -eq $value) {
        $path = Join-Path $ProjectDirectory '.env'
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            foreach ($line in [System.IO.File]::ReadAllLines($path)) {
                if ($line.TrimStart().StartsWith('#') -or -not $line.Contains('=')) { continue }
                $separator = $line.IndexOf('=')
                if ($line.Substring(0, $separator).Trim() -eq 'ZJU_WIFI_FAILOVER') {
                    $value = $line.Substring($separator + 1)
                    break
                }
            }
        }
    }
    if ($null -eq $value) { return $false }
    $value = $value.Trim()
    if ($value -notin @('0', '1')) { throw 'ZJU_WIFI_FAILOVER must be 0 or 1.' }
    return ($value -eq '1')
}
