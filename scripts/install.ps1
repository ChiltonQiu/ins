<#
.SYNOPSIS
    Everything between a clone and a running application, on Windows.

.DESCRIPTION
    The PowerShell counterpart of scripts/install.sh, and it does the same
    things in the same order: checks the machine, builds the virtualenv,
    writes a .env, generates a blob encryption key if there is none, creates
    the database, runs the migrations, and offers to make the first account.

    Safe to re-run. It never overwrites a .env, never regenerates an
    encryption key that already exists, and re-running after a git pull is how
    you upgrade.

    It does not install anything. Python, PostgreSQL and Tesseract are
    decisions about a machine; this names the download and stops.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1

    The ExecutionPolicy flag is usually needed: a freshly downloaded script is
    blocked by default, and the flag applies to this one process rather than
    changing the machine's policy.
#>

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')

function Say  { param($m) Write-Host "`n$m" -ForegroundColor White }
function Ok   { param($m) Write-Host "  [ok] $m" -ForegroundColor Green }
function Warn { param($m) Write-Host "  [!]  $m" -ForegroundColor Yellow }
function Die  { param($m) Write-Host "`n$m`n" -ForegroundColor Red; exit 1 }

# Windows ships tesseract.exe and the PostgreSQL tools outside PATH more often
# than not, so "not on PATH" is not the same question as "not installed".
function Find-Tool {
    param([string]$Name, [string[]]$Candidates)
    $found = Get-Command $Name -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    foreach ($pattern in $Candidates) {
        $hit = Get-ChildItem -Path $pattern -ErrorAction SilentlyContinue |
               Sort-Object FullName -Descending | Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    return $null
}

# ----------------------------------------------------------------- the machine

Say 'Checking what this machine already has'

# Kept as an executable plus its arguments rather than one string: the py
# launcher needs '-3' and everything else does not, and a string that is
# sometimes two tokens has to be re-parsed at every call site.
$pyExe = $null
$pyArgs = @()
foreach ($candidate in @('python', 'python3', 'py')) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    # The App Execution Alias in WindowsApps is a stub that opens the Store
    # rather than running anything, and it answers `python` on a machine with
    # no Python at all.
    if ($cmd.Source -like '*WindowsApps*') { continue }
    $pyExe = $cmd.Source
    if ($candidate -eq 'py') { $pyArgs = @('-3') }
    break
}
if (-not $pyExe) {
    Die @'
Python is not installed, or only the Microsoft Store stub is.
  Download: https://www.python.org/downloads/windows/
  Tick "Add python.exe to PATH" in the installer, then run this again.
'@
}

$versionOk = & $pyExe @pyArgs -c 'import sys; print(sys.version_info >= (3, 11))'
if ($versionOk -ne 'True') {
    Die "Python 3.11 or newer is required; this is $(& $pyExe @pyArgs -V)."
}
Ok (& $pyExe @pyArgs -V)

$psql = Find-Tool 'psql.exe' @('C:\Program Files\PostgreSQL\*\bin\psql.exe')
if (-not $psql) {
    Die @'
PostgreSQL is not installed.
  Download: https://www.postgresql.org/download/windows/
Install it, remember the password you set for the postgres user, then run
this again.
'@
}
$pgBin = Split-Path $psql
Ok "PostgreSQL tools at $pgBin"
# Everything below shells out to these by full path, because the installer
# does not put them on PATH and requiring a terminal restart mid-script is a
# poor way to find that out.
$createdb = Join-Path $pgBin 'createdb.exe'

# A scan without OCR is still stored, hashed and linked — it simply has no
# text layer, so it is searchable only by filename and no dates come off it.
# Worth a warning rather than a stop.
$tesseract = Find-Tool 'tesseract.exe' @(
    'C:\Program Files\Tesseract-OCR\tesseract.exe',
    'C:\Program Files (x86)\Tesseract-OCR\tesseract.exe',
    "$env:LOCALAPPDATA\Programs\Tesseract-OCR\tesseract.exe"
)
if ($tesseract) {
    Ok "Tesseract at $tesseract"
} else {
    Warn 'Tesseract is not installed. Scanned documents will be stored but not read.'
    Warn '  Download: https://github.com/UB-Mannheim/tesseract/wiki'
}

# ------------------------------------------------------------------- the venv

Say 'Installing the application'

if (-not (Test-Path '.venv')) {
    & $pyExe @pyArgs -m venv .venv
    if ($LASTEXITCODE -ne 0) { Die 'Creating the virtualenv failed.' }
    Ok 'Created .venv'
} else {
    Ok 'Using the existing .venv'
}

$venvPython = (Resolve-Path '.venv\Scripts\python.exe').Path
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet -e '.[dev]'
if ($LASTEXITCODE -ne 0) { Die 'Installing the dependencies failed.' }
Ok 'Dependencies installed'

# ------------------------------------------------------------------- config

Say 'Configuration'

# UTF-8 with no BOM, deliberately. PowerShell 5.1's Set-Content -Encoding UTF8
# writes a BOM, python-dotenv reads it as part of the first key's name, and
# the first setting in the file silently stops existing.
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
function Write-EnvFile { param([string[]]$Lines)
    [System.IO.File]::WriteAllText(
        (Join-Path $PWD '.env'), ($Lines -join "`r`n") + "`r`n", $utf8NoBom)
}

if (-not (Test-Path '.env')) {
    Copy-Item '.env.example' '.env'
    Ok 'Wrote .env from .env.example'
} else {
    Ok '.env already exists, left alone'
}

$envLines = @(Get-Content '.env')
function Get-EnvValue { param([string]$Key)
    $line = $envLines | Where-Object { $_ -match "^$([regex]::Escape($Key))=" } | Select-Object -Last 1
    if ($line) { return $line.Substring($Key.Length + 1).Trim() }
    return ''
}
function Set-EnvValue { param([string]$Key, [string]$Value)
    $script:envLines = if ($envLines -match "^$([regex]::Escape($Key))=") {
        $envLines | ForEach-Object {
            if ($_ -match "^$([regex]::Escape($Key))=") { "$Key=$Value" } else { $_ }
        }
    } else {
        $envLines + "$Key=$Value"
    }
    Write-EnvFile $script:envLines
}

# A key that appears by magic is a key nobody knows they have to back up. It is
# written only when the line is empty: regenerating one makes every existing
# blob unreadable, permanently.
if (-not (Get-EnvValue 'BLOB_ENCRYPTION_KEY')) {
    $key = & $venvPython -c 'from renewal.crypto import generate_key; print(generate_key())'
    Set-EnvValue 'BLOB_ENCRYPTION_KEY' $key
    Warn 'Generated BLOB_ENCRYPTION_KEY and wrote it to .env.'
    Warn 'BACK IT UP somewhere your blob backups are not. Lose it and every'
    Warn 'document is unreadable, by anyone, permanently.'
} else {
    Ok 'BLOB_ENCRYPTION_KEY is set'
}

# pytesseract looks on PATH and the Windows installer does not put it there,
# so the binary would be present and unreachable. Naming it in .env is what
# makes OCR work on this machine.
if ($tesseract -and -not (Get-EnvValue 'TESSERACT_CMD')) {
    Set-EnvValue 'TESSERACT_CMD' $tesseract
    Ok 'Pointed TESSERACT_CMD at the binary'
}

# ----------------------------------------------------------------- database

Say 'Database'

$dbUrl = Get-EnvValue 'DATABASE_URL'
if (-not $dbUrl) { $dbUrl = 'postgresql+psycopg:///renewal' }
$dbName = ($dbUrl -split '/')[-1] -replace '\?.*$', ''

$existing = & $psql -lqt 2>$null
if ($LASTEXITCODE -ne 0) {
    Die @"
Could not reach PostgreSQL with $psql.
It is installed but not answering. Check the service is running:
  Get-Service postgresql*
and that PGUSER / PGPASSWORD are set, or that your user can connect without
a password. A connection this script cannot make is one the application
cannot make either.
"@
}
if (($existing -split "`n" | ForEach-Object { ($_ -split '\|')[0].Trim() }) -contains $dbName) {
    Ok "Database '$dbName' exists"
} else {
    & $createdb $dbName 2>$null
    if ($LASTEXITCODE -ne 0) {
        Die @"
Could not create the database '$dbName'.
Your PostgreSQL user probably cannot create databases. Either create it in
pgAdmin, or point DATABASE_URL in .env at a database that already exists.
"@
    }
    Ok "Created database '$dbName'"
}

& $venvPython -m alembic upgrade head | Out-Null
if ($LASTEXITCODE -ne 0) { Die 'The migrations failed. The error is above.' }
Ok 'Migrations are at head'

# ------------------------------------------------------------------- model

Say 'Model provider'

$provider = Get-EnvValue 'PROVIDER'
if (-not $provider) { $provider = 'anthropic' }
$keyVar = switch ($provider) {
    'anthropic'   { 'ANTHROPIC_API_KEY' }
    'openai'      { 'OPENAI_API_KEY' }
    'grok'        { 'XAI_API_KEY' }
    'huggingface' { 'HF_TOKEN' }
    'custom'      { 'LLM_API_KEY' }
    default       { '' }
}

$modelReady = $true
if ($keyVar -and -not (Get-EnvValue $keyVar)) {
    $modelReady = $false
    Warn "PROVIDER=$provider but $keyVar is empty in .env."
    Warn 'The application builds its model client at import, so it will not'
    Warn 'start until that is filled in.'
} else {
    Ok "PROVIDER=$provider"
}

# ----------------------------------------------------------------- accounts

Say 'Accounts'

# Written to a file rather than passed to -c: a multi-line argument has to
# survive PowerShell's quoting and then the Windows command line's, and there
# is no reason to ask it to.
$countScript = Join-Path ([System.IO.Path]::GetTempPath()) 'renewal-count-accounts.py'
Set-Content -Path $countScript -Encoding ASCII -Value @'
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker
from renewal.db import get_engine
from renewal.models import User
session = sessionmaker(bind=get_engine())()
print(session.scalar(select(func.count(User.id))))
'@
$accounts = & $venvPython $countScript
Remove-Item $countScript -ErrorAction SilentlyContinue
if ($LASTEXITCODE -ne 0) { Die 'Could not read the accounts table.' }

if ([int]$accounts -gt 0) {
    Ok "$accounts account(s) already exist"
} elseif ([Console]::IsInputRedirected -or -not [Environment]::UserInteractive) {
    # The .exe runs this through nsExec, which has no console to read from, so
    # Read-Host here returns nothing and the first account is silently never
    # made — leaving somebody at a login page with no way past it. Say so
    # instead, and leave create-account.cmd for them to double-click.
    Warn 'No account yet, and nothing here to type into.'
    Warn 'Double-click create-account.cmd in this folder to make one.'
} else {
    # There is no signup route on purpose: the person with shell access is the
    # provisioning system. This is that person, here, now.
    Write-Host "`n  No accounts yet. Make the first one."
    $email = Read-Host '  Email address'
    if ($email) {
        & $venvPython -m scripts.add_user $email
    } else {
        Warn 'Skipped. Double-click create-account.cmd to make one.'
    }
}

# ------------------------------------------------------- a way to start it

Say 'Starting it'

# Everything above this point leaves somebody with a working application and
# no way to run it that does not involve typing. That is the actual barrier on
# Windows — not the install, the sixty times afterwards.
$startScript = Join-Path $PWD 'scripts\start.ps1'
$launcher = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$startScript`""

function New-RenewalShortcut {
    param([string]$Folder)
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut((Join-Path $Folder 'Renewal.lnk'))
    $link.TargetPath = (Get-Command powershell).Source
    $link.Arguments = $launcher
    $link.WorkingDirectory = "$PWD"
    $link.Description = 'Open Renewal'
    # A generic document icon from the shell library: no icon at all gives a
    # blue PowerShell square, which reads as "a script somebody left here".
    $link.IconLocation = "$env:SystemRoot\System32\imageres.dll,3"
    $link.Save()
}

try {
    New-RenewalShortcut ([Environment]::GetFolderPath('Desktop'))
    $startMenu = Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs'
    if (Test-Path $startMenu) { New-RenewalShortcut $startMenu }
    Ok 'Desktop and Start Menu shortcut created'
} catch {
    Warn "Could not create the shortcut: $($_.Exception.Message)"
    Warn "Start it by hand with: powershell -ExecutionPolicy Bypass -File scripts\start.ps1"
}

# The Windows half of deploy/renewal.service. A logon task rather than a
# service: a service would need an account to run as and a password to go with
# it, and this is one person's computer.
if ($env:RENEWAL_AUTOSTART -eq '0') {
    Ok 'Skipped the logon task (RENEWAL_AUTOSTART=0)'
} else {
    try {
        $action = New-ScheduledTaskAction `
            -Execute (Get-Command powershell).Source `
            -Argument "$launcher -NoBrowser" `
            -WorkingDirectory "$PWD"
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        # A laptop that was on battery at sign-in is still a laptop somebody
        # is about to work on, and the default settings would skip the run.
        $taskSettings = New-ScheduledTaskSettingsSet `
            -StartWhenAvailable -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries
        Register-ScheduledTask -TaskName 'Renewal' -Action $action `
            -Trigger $trigger -Settings $taskSettings -Force | Out-Null
        Ok 'It will start itself when this account signs in'
    } catch {
        Warn "Could not register the logon task: $($_.Exception.Message)"
        Warn 'The desktop shortcut still works.'
    }
}

# --------------------------------------------------------------------- done

Say 'Ready'

if (-not $modelReady) {
    Write-Host "  1. Put $keyVar in .env - nothing starts without it."
    Write-Host '  2. Double-click the Renewal icon on the desktop.'
} else {
    Write-Host '  Double-click the Renewal icon on the desktop.'
    Write-Host '  It opens at http://127.0.0.1:8000, and starts itself at sign-in.'
}

Write-Host @'

  Optional, and each one is a section in the README:
    Mail        the six IMAP_* values, so documents arrive on their own
    Summaries   the SMTP_* values, so the daily email can be sent
    An archive  .venv\Scripts\python -m scripts.bulk_import C:\path\to\archive

  To stop it starting at sign-in:  schtasks /Delete /TN Renewal /F

'@
