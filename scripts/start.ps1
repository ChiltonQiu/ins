<#
.SYNOPSIS
    Start the application and open it, or just open it if it is already up.

.DESCRIPTION
    What the desktop shortcut runs, and what the logon task runs with
    -NoBrowser. Written so that running it twice is harmless: if something is
    already listening on the port, this opens a browser at it rather than
    starting a second copy that cannot bind and dies confusingly.

    The server runs in a hidden window. It keeps running when the browser is
    closed, which is the behaviour somebody expects from a thing they started
    from a desktop icon, and it stops when the machine does.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\start.ps1
    powershell -ExecutionPolicy Bypass -File scripts\start.ps1 -NoBrowser
#>

[CmdletBinding()]
param(
    [int]$Port = 8000,
    [switch]$NoBrowser,
    # For the logon task: the network stack and PostgreSQL are often not ready
    # the instant somebody signs in, and a service that gave up at second one
    # would look like a service that does not work.
    [int]$WaitSeconds = 40
)

$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')

$url = "http://127.0.0.1:$Port"

function Test-Up {
    # /login is served to everybody and answers 200, so there is no redirect
    # to reason about — which matters, because the exception a redirect raises
    # is a different type in PowerShell 5.1 and in 7. Anything that throws
    # here means nothing is listening yet.
    try {
        $probe = Invoke-WebRequest -Uri "$url/login" -UseBasicParsing `
            -TimeoutSec 3 -ErrorAction Stop
        return $probe.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Say-Problem {
    # The shortcut runs this with the window hidden, so Write-Host goes
    # nowhere anybody can see. WScript.Shell is already how the installer
    # makes the shortcut and needs no assembly loaded.
    param([string]$Message)
    Write-Host $Message -ForegroundColor Red
    try {
        (New-Object -ComObject WScript.Shell).Popup($Message, 0, 'Renewal', 0x10) | Out-Null
    } catch {
        # No shell object available: the console line above is the whole of it.
    }
}

if (-not (Test-Up)) {
    $python = Join-Path $PWD '.venv\Scripts\python.exe'
    if (-not (Test-Path $python)) {
        Say-Problem 'Renewal is not installed yet. Run install.cmd in this folder first.'
        exit 1
    }

    Start-Process -FilePath $python `
        -ArgumentList @('-m', 'uvicorn', 'renewal.app:app',
                        '--host', '127.0.0.1', '--port', "$Port") `
        -WorkingDirectory $PWD -WindowStyle Hidden

    $deadline = (Get-Date).AddSeconds($WaitSeconds)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 700
        if (Test-Up) { break }
    }

    if (-not (Test-Up)) {
        Say-Problem @"
Renewal did not start within $WaitSeconds seconds.

The usual cause is an empty ANTHROPIC_API_KEY in .env: the application builds
its model client when it loads, so it stops before it serves anything.

To see the actual error, open this folder in a terminal and run
  .venv\Scripts\python -m uvicorn renewal.app:app --port $Port
"@
        exit 1
    }
}

if (-not $NoBrowser) {
    Start-Process $url
}
