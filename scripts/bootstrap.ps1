<#
.SYNOPSIS
    Install the things this application needs, then install the application.

.DESCRIPTION
    scripts\install.ps1 checks for Python and PostgreSQL and stops with a
    download link when one is missing. This does the downloading, so that
    somebody who has neither ends up with a working application without ever
    opening an installer or being asked for an administrator password.

    Nothing here needs elevation, which is the whole design:

      Python      per-user install (InstallAllUsers=0), into AppData.
      PostgreSQL  the binaries zip rather than the installer — unpacked into
                  this folder, with its own data directory and its own port.
                  No Windows service, no superuser password, nothing
                  registered on the machine.

    Both are skipped when the machine already has them. A system PostgreSQL is
    used as-is; only a machine without one gets a private cluster.

    Deleting this folder removes everything except the per-user Python.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1
#>

[CmdletBinding()]
param(
    [string]$PythonVersion = '3.12.9',
    [string]$PostgresVersion = '16.6-1',
    # 5433 rather than 5432: a machine that already has PostgreSQL is using
    # the standard port, and a private cluster must not fight it for one.
    [int]$PostgresPort = 5433,
    [switch]$SkipApp
)

$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
$root = $PWD

function Say  { param($m) Write-Host "`n$m" -ForegroundColor White }
function Ok   { param($m) Write-Host "  [ok] $m" -ForegroundColor Green }
function Warn { param($m) Write-Host "  [!]  $m" -ForegroundColor Yellow }
function Die  { param($m) Write-Host "`n$m`n" -ForegroundColor Red; exit 1 }

$runtime = Join-Path $root 'runtime'
New-Item -ItemType Directory -Force -Path $runtime | Out-Null
$downloads = Join-Path $runtime 'downloads'
New-Item -ItemType Directory -Force -Path $downloads | Out-Null

function Get-File {
    param([string]$Url, [string]$Destination, [string]$What)
    if (Test-Path $Destination) {
        Ok "$What already downloaded"
        return
    }
    Write-Host "  downloading $What ..." -NoNewline
    $partial = "$Destination.part"
    try {
        # The progress bar costs more than the download on PowerShell 5.1:
        # Invoke-WebRequest redraws it per chunk and a 290 MB file takes
        # minutes longer with it on than off.
        $previous = $ProgressPreference
        $ProgressPreference = 'SilentlyContinue'
        Invoke-WebRequest -Uri $Url -OutFile $partial -UseBasicParsing
        $ProgressPreference = $previous
        Move-Item $partial $Destination -Force
        Write-Host " done"
    } catch {
        Remove-Item $partial -ErrorAction SilentlyContinue
        Die "Could not download $What.`n$($_.Exception.Message)`n`nURL: $Url"
    }
}

# ------------------------------------------------------------------- Python

Say 'Python'

function Find-Python {
    foreach ($candidate in @('python', 'python3', 'py')) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        # The App Execution Alias in WindowsApps opens the Store instead of
        # running anything, and it answers `python` on a machine with none.
        if ($cmd.Source -like '*WindowsApps*') { continue }
        $args = if ($candidate -eq 'py') { @('-3') } else { @() }
        try {
            $ok = & $cmd.Source @args -c 'import sys; print(sys.version_info >= (3, 11))'
        } catch { continue }
        if ($ok -eq 'True') { return @{ Exe = $cmd.Source; Args = $args } }
    }
    return $null
}

$python = Find-Python
if ($python) {
    Ok "Using the Python already installed"
} else {
    $installer = Join-Path $downloads "python-$PythonVersion-amd64.exe"
    Get-File "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe" `
        $installer "Python $PythonVersion (25 MB)"

    Write-Host '  installing Python for this user ...' -NoNewline
    # InstallAllUsers=0 keeps it in AppData and out of Program Files, which is
    # what makes this work without an administrator password.
    $proc = Start-Process -FilePath $installer -Wait -PassThru -ArgumentList @(
        '/passive', 'InstallAllUsers=0', 'PrependPath=1',
        'Include_test=0', 'Include_doc=0', 'AssociateFiles=0'
    )
    Write-Host " done"
    if ($proc.ExitCode -ne 0) {
        Die "The Python installer exited with code $($proc.ExitCode)."
    }

    # PrependPath only affects processes started afterwards, so this one still
    # cannot see it. Look where a per-user install actually lands.
    $short = $PythonVersion -replace '^(\d+)\.(\d+).*$', '$1$2'
    $guess = Join-Path $env:LOCALAPPDATA "Programs\Python\Python$short\python.exe"
    if (Test-Path $guess) {
        $python = @{ Exe = $guess; Args = @() }
    } else {
        $env:Path = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + $env:Path
        $python = Find-Python
    }
    if (-not $python) {
        Die "Python installed but could not be found afterwards. Close this window, open a new one, and run install.cmd again — the PATH change needs a fresh terminal."
    }
    Ok "Python $PythonVersion installed for this user"
}

# --------------------------------------------------------------- PostgreSQL

Say 'PostgreSQL'

$pgBinLocal = Join-Path $runtime 'pgsql\bin'
$pgData = Join-Path $runtime 'pgdata'
$systemPsql = Get-Command 'psql.exe' -ErrorAction SilentlyContinue
if (-not $systemPsql) {
    $found = Get-ChildItem 'C:\Program Files\PostgreSQL\*\bin\psql.exe' -ErrorAction SilentlyContinue |
             Sort-Object FullName -Descending | Select-Object -First 1
    if ($found) { $systemPsql = @{ Source = $found.FullName } }
}

$usePrivate = $false
if ($systemPsql -and -not (Test-Path $pgBinLocal)) {
    Ok "Using the PostgreSQL already installed"
    $pgBin = Split-Path $systemPsql.Source
} else {
    $usePrivate = $true
    $pgBin = $pgBinLocal
    if (-not (Test-Path (Join-Path $pgBin 'postgres.exe'))) {
        $zip = Join-Path $downloads "postgresql-$PostgresVersion-windows-x64-binaries.zip"
        Get-File "https://get.enterprisedb.com/postgresql/postgresql-$PostgresVersion-windows-x64-binaries.zip" `
            $zip "PostgreSQL $PostgresVersion (290 MB — this is the slow part)"
        Write-Host '  unpacking ...' -NoNewline
        # The archive contains a single pgsql/ directory, which lands as
        # runtime\pgsql exactly where the paths above expect it.
        Expand-Archive -Path $zip -DestinationPath $runtime -Force
        Write-Host " done"
    }
    Ok 'PostgreSQL unpacked into this folder (no service, no admin)'

    if (-not (Test-Path (Join-Path $pgData 'PG_VERSION'))) {
        Write-Host '  creating the database cluster ...' -NoNewline
        # trust, and listening on loopback only: there is no account to
        # authenticate against on a cluster that exists for one application on
        # one laptop, and a password here would have to be stored beside it.
        $initdb = Join-Path $pgBin 'initdb.exe'
        & $initdb -D $pgData -U postgres --auth=trust --encoding=UTF8 -E UTF8 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { Die 'initdb failed.' }
        Add-Content (Join-Path $pgData 'postgresql.conf') @"

# Written by scripts\bootstrap.ps1
listen_addresses = '127.0.0.1'
port = $PostgresPort
"@
        Write-Host " done"
        Ok "Cluster created in runtime\pgdata on port $PostgresPort"
    }

    # Idempotent: pg_ctl start on a running cluster reports it and exits
    # non-zero, which is not a failure worth stopping for.
    $pgCtl = Join-Path $pgBin 'pg_ctl.exe'
    & $pgCtl -D $pgData -l (Join-Path $runtime 'postgres.log') -w start 2>&1 | Out-Null
    Start-Sleep -Seconds 2

    $createdb = Join-Path $pgBin 'createdb.exe'
    & $createdb -h 127.0.0.1 -p $PostgresPort -U postgres renewal 2>&1 | Out-Null
    Ok 'Database ready'
}

# ------------------------------------------------------------------ the .env

Say 'Configuration'

if (-not (Test-Path '.env')) {
    Copy-Item '.env.example' '.env'
    Ok 'Wrote .env from .env.example'
}

if ($usePrivate) {
    # install.ps1 reads DATABASE_URL to decide which database to create and
    # migrate, so the private cluster has to be named here before it runs.
    $url = "postgresql+psycopg://postgres@127.0.0.1:$PostgresPort/renewal"
    $lines = @(Get-Content '.env')
    if ($lines -match '^DATABASE_URL=') {
        $lines = $lines | ForEach-Object {
            if ($_ -match '^DATABASE_URL=') { "DATABASE_URL=$url" } else { $_ }
        }
    } else {
        $lines += "DATABASE_URL=$url"
    }
    [System.IO.File]::WriteAllText(
        (Join-Path $root '.env'), ($lines -join "`r`n") + "`r`n",
        (New-Object System.Text.UTF8Encoding $false))
    Ok "DATABASE_URL points at the private cluster"
}

# ------------------------------------------------------------- the application

if ($SkipApp) {
    Say 'Stopping here (-SkipApp)'
    exit 0
}

Say 'Installing the application'

# In a child process on purpose. `& script.ps1` is a script call, and a script
# call does not set $LASTEXITCODE — so the old `exit $LASTEXITCODE` here
# propagated whatever the last *native* command inside install.ps1 happened to
# leave behind, and reported a successful install as a failure. Run as a
# native command, the exit code is install.ps1's own.
$installer = Join-Path $PSScriptRoot 'install.ps1'
& (Get-Command powershell).Source -NoProfile -ExecutionPolicy Bypass -File $installer
$code = $LASTEXITCODE
if ($code -ne 0) { exit $code }
exit 0
