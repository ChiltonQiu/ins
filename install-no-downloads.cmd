@echo off
rem The installer without the downloads.
rem
rem install.cmd fetches Python and PostgreSQL when they are missing. This one
rem assumes the machine already has both and fails with a download link if it
rem does not. Use it on a machine that is already set up, or when you would
rem rather install the prerequisites yourself.

setlocal
echo.
echo   Installing Renewal (using the Python and PostgreSQL already here).
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" %*
set RESULT=%ERRORLEVEL%

echo.
if %RESULT% NEQ 0 (
    echo   Something went wrong. The error is above.
) else (
    echo   Done. Close this window.
)
echo.
pause
exit /b %RESULT%
