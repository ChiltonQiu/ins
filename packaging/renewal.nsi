; The Windows installer.
;
; Built on Linux with makensis, which is the reason this is NSIS and not Inno
; Setup or WiX:
;
;     sudo pacman -S nsis          # or: apt install nsis
;     makensis -DVERSION=0.2.1 packaging/renewal.nsi
;
; It installs per-user into %LOCALAPPDATA%, so it never asks for an
; administrator password — and neither does anything it runs. The application
; it unpacks is the same tree as the source distribution; the work of finding
; or fetching Python and PostgreSQL belongs to scripts\bootstrap.ps1 and is
; not duplicated here.

!ifndef VERSION
  !define VERSION "0.0.0"
!endif

!define APPNAME "Renewal"
!define PUBLISHER "Renewal"

Unicode true
Name "${APPNAME} ${VERSION}"
OutFile "..\dist\Renewal-${VERSION}-setup.exe"
; Per-user, so no elevation prompt and no Program Files.
RequestExecutionLevel user
InstallDir "$LOCALAPPDATA\${APPNAME}"
InstallDirRegKey HKCU "Software\${APPNAME}" "InstallDir"
ShowInstDetails show
SetCompressor /SOLID lzma

!include "MUI2.nsh"
!include "LogicLib.nsh"

!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TITLE "Install ${APPNAME}"
!define MUI_WELCOMEPAGE_TEXT "This installs Renewal for your account only. It does not need an administrator password.$\r$\n$\r$\nIf this computer has no Python or PostgreSQL, the installer downloads them (about 315 MB) and sets them up inside the installation folder. On a slow connection that part can take ten minutes.$\r$\n$\r$\nNothing is registered on the machine and no Windows service is created. Uninstalling removes the folder."

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_TEXT "Renewal is installed.$\r$\n$\r$\nTwo things are still needed before it will start:$\r$\n$\r$\n1. An API key in the .env file in the installation folder.$\r$\n2. A login. Tick the box below to make one now, or double-click create-account.cmd later.$\r$\n$\r$\nAfter that, use the Renewal icon on the desktop."
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_FUNCTION MakeAccount
!define MUI_FINISHPAGE_RUN_TEXT "Create a login now"
!define MUI_FINISHPAGE_SHOWREADME
!define MUI_FINISHPAGE_SHOWREADME_FUNCTION OpenFolder
!define MUI_FINISHPAGE_SHOWREADME_TEXT "Open the installation folder"
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Function OpenFolder
  ExecShell "open" "$INSTDIR"
FunctionEnd

Function MakeAccount
  ; A visible console window on purpose: it has to read an address and a
  ; password from somebody, which is exactly what the installer itself cannot
  ; do — and why the account step was skipped in the first place.
  ExecShell "open" "$INSTDIR\create-account.cmd"
FunctionEnd

Section "Renewal" SecMain
  SetOutPath "$INSTDIR"

  ; The application tree, staged by packaging/build-installer.sh from the
  ; source distribution so that what ships here and what ships in the tarball
  ; cannot drift apart.
  File /r "..\dist\stage\*.*"

  WriteRegStr HKCU "Software\${APPNAME}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\${APPNAME}" "Version" "${VERSION}"

  ; Add/Remove Programs, per-user.
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" \
      "DisplayName" "${APPNAME} ${VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" \
      "UninstallString" "$\"$INSTDIR\uninstall.exe$\""
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" \
      "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" \
      "Publisher" "${PUBLISHER}"
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" \
      "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" \
      "NoRepair" 1
  WriteUninstaller "$INSTDIR\uninstall.exe"

  ; Everything real happens here. The window is shown rather than hidden: this
  ; step can run for ten minutes on a slow connection, and an installer that
  ; appears to have frozen is one somebody kills halfway through creating a
  ; database cluster.
  DetailPrint "Setting up Python, PostgreSQL and the application..."
  nsExec::ExecToLog 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\scripts\bootstrap.ps1"'
  Pop $0
  ${If} $0 != 0
    DetailPrint "Setup did not finish cleanly (exit code $0)."
    DetailPrint "The log is at $INSTDIR\runtime\install.log"
    MessageBox MB_ICONEXCLAMATION|MB_OK "Setup did not finish.$\r$\n$\r$\nWhat went wrong is written to:$\r$\n$INSTDIR\runtime\install.log$\r$\n$\r$\nSend that file to whoever is helping you. To try again with the output on screen, run install.cmd in:$\r$\n$INSTDIR"
  ${EndIf}
SectionEnd

Section "Uninstall"
  ; The logon task, if bootstrap registered one.
  nsExec::ExecToLog 'schtasks /Delete /TN "Renewal" /F'
  Pop $0

  Delete "$DESKTOP\Renewal.lnk"
  Delete "$SMPROGRAMS\Renewal.lnk"

  ; Everything this application is — the code, the virtualenv, the private
  ; database cluster and every document stored in the blob directory — lives
  ; under one folder, so uninstalling is removing it. Which is also why this
  ; asks first.
  MessageBox MB_YESNO|MB_ICONEXCLAMATION \
      "Remove everything in $INSTDIR?$\r$\n$\r$\nThis includes the database and every document that has been filed. If you want to keep them, say No and copy the folder somewhere first." \
      IDYES removeAll
  Goto keepData

  removeAll:
    RMDir /r "$INSTDIR"
  keepData:

  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}"
  DeleteRegKey HKCU "Software\${APPNAME}"
SectionEnd
