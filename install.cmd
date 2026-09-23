@echo off
rem Double-click this.
rem
rem A .ps1 cannot be run by double-clicking it — Windows opens it in an editor,
rem and a downloaded one is blocked by the execution policy besides. A .cmd
rem can, so this is the one file somebody has to find in the folder.
rem
rem -ExecutionPolicy Bypass applies to this one process and changes nothing
rem about the machine.

setlocal
echo.
echo   Installing Renewal.
echo.
echo   On a machine with neither Python nor PostgreSQL this downloads both
echo   ^(about 315 MB^) and can take ten minutes. Nothing needs an administrator.
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\bootstrap.ps1" %*
set RESULT=%ERRORLEVEL%

echo.
if %RESULT% NEQ 0 (
    echo   Something went wrong. The error is above.
) else (
    echo   Done. Close this window.
)
echo.
rem Without this the window vanishes the instant it finishes, taking the
rem instructions — and any error — with it.
pause
exit /b %RESULT%
