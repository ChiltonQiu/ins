@echo off
rem Make a login for this application, or reset one.
rem
rem The installer cannot do this itself: it runs with no console to type into,
rem so the password would have nowhere to come from. The password is never
rem passed as an argument — it would land in the command history and be
rem visible to every other account on the machine.

setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo   Renewal is not installed in this folder yet.
    echo   Run install.cmd first.
    echo.
    pause
    exit /b 1
)

echo.
set /p EMAIL=  Email address for the login:
if "%EMAIL%"=="" (
    echo   No address given; nothing to do.
    echo.
    pause
    exit /b 1
)

echo.
".venv\Scripts\python.exe" -m scripts.add_user "%EMAIL%"
set RESULT=%ERRORLEVEL%

echo.
if %RESULT% EQU 0 (
    echo   Done. Use the Renewal icon on the desktop and sign in with that address.
) else (
    echo   That did not work. The reason is above.
)
echo.
pause
exit /b %RESULT%
