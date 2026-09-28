<#
    Install a downloaded release over this one, then start it again.

    Started by the application itself (renewal/updates.py), detached, with the
    application's own process id. In order: back up the private database, stop
    the application, run the installer silently into this folder, start the
    application again -- whether or not the install worked, so a failed update
    leaves the old version running rather than nothing. Everything goes to
    runtime\update.log.
#>
param(
    [Parameter(Mandatory = $true)][string]$Installer,
    [int]$AppPid = 0
)

$ErrorActionPreference = 'Stop'

function Invoke-Native {
    # Windows PowerShell 5.1 turns each line a native command writes to
    # stderr into an error record once stderr is redirected, and under
    # ErrorActionPreference = 'Stop' that record is fatal. Relaxed here, for
    # this call alone; $LASTEXITCODE is still the command's own.
    param([scriptblock]$Command)
    $ErrorActionPreference = 'Continue'
    & $Command 2>&1 | ForEach-Object { "$_" }
}

Set-Location (Join-Path $PSScriptRoot '..')
$root = $PWD.Path
$runtime = Join-Path $root 'runtime'
New-Item -ItemType Directory -Force -Path $runtime | Out-Null
$logFile = Join-Path $runtime 'update.log'

# Each line is appended to the file itself, not left to a transcript: this
# runs with no one watching, and a transcript that failed to start would
# take every line down with it.
function Log {
    param($m)
    $line = "$(Get-Date -Format s)  $m"
    Write-Output $line
    try { Add-Content -Path $logFile -Value $line -Encoding UTF8 } catch { }
}

trap {
    Log "update.ps1 failed: $($_.Exception.Message) at line $($_.InvocationInfo.ScriptLineNumber)"
    exit 1
}

Log "update: installer $Installer, application pid $AppPid"

# ------------------------------------------------------------------ backup

$pgDump = Join-Path $runtime 'pgsql\bin\pg_dump.exe'
if (Test-Path $pgDump) {
    $conf = Join-Path $runtime 'pgdata\postgresql.conf'
    $port = 5433
    $found = Select-String -Path $conf -Pattern '^\s*port\s*=\s*(\d+)' -ErrorAction SilentlyContinue |
             Select-Object -Last 1
    if ($found) { $port = [int]$found.Matches[0].Groups[1].Value }
    $backups = Join-Path $runtime 'backups'
    New-Item -ItemType Directory -Force -Path $backups | Out-Null
    $file = Join-Path $backups ("before-update-{0}.dump" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
    Remove-Item Env:PGPORT, Env:PGDATA, Env:PGPASSWORD -ErrorAction SilentlyContinue
    $out = Invoke-Native { & $pgDump -h 127.0.0.1 -p $port -U postgres -Fc -f $file renewal }
    if ($LASTEXITCODE -ne 0) {
        Log "backup failed ($LASTEXITCODE): $out"
        Log 'not updating without a backup; the running version is untouched'
        exit 1
    }
    Log "backup: $file"
} else {
    Log 'backup skipped: this install uses a PostgreSQL outside this folder'
}

# -------------------------------------------------------------------- stop

if ($AppPid -gt 0) {
    try {
        Stop-Process -Id $AppPid -Force -ErrorAction Stop
        Wait-Process -Id $AppPid -Timeout 30 -ErrorAction SilentlyContinue
        Log "stopped the application ($AppPid)"
    } catch {
        Log "the application ($AppPid) was not running: $($_.Exception.Message)"
    }
}

# ------------------------------------------------------------------ install

# /D= must be last and unquoted, spaces and all: that is how NSIS reads it.
# WaitForExit waits for the installer alone -- not for the database it may
# start, which Start-Process -Wait would wait on forever.
$proc = Start-Process -FilePath $Installer -ArgumentList @('/S', "/D=$root") -PassThru
$null = $proc.Handle
$proc.WaitForExit()
$code = $proc.ExitCode
Log "installer exited $code"

# -------------------------------------------------------------------- start

& (Get-Command powershell).Source -NoProfile -ExecutionPolicy Bypass `
    -File (Join-Path $root 'scripts\start.ps1') -NoBrowser
Log "start exited $LASTEXITCODE"
exit $code
