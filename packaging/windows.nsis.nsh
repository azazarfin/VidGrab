; The checker is compiled beside the desktop, then embedded in setup and its
; uninstaller. Destination executables never provide their own trust decision.
!macro VIDGRAB_CHECK Operation Directory
  InitPluginsDir
  !searchreplace VidGrabChecker "${MAINBINARYSRCPATH}" "${MAINBINARYNAME}.exe" "vidgrab-supervisor.exe"
  File "/oname=$PLUGINSDIR\vidgrab-installer-check.exe" "${VidGrabChecker}"
  !undef VidGrabChecker
  nsExec::ExecToStack /TIMEOUT=60000 '"$PLUGINSDIR\vidgrab-installer-check.exe" ${Operation} "${Directory}"'
  Pop $0
  Pop $1
  ${If} $0 != 0
    IfSilent +2 0
    MessageBox MB_OK|MB_ICONEXCLAMATION "VidGrab cannot change these installed files.$\r$\nFinish active downloads, or restore changed files before continuing.$\r$\n$1"
    SetErrorLevel 2
    Abort
  ${EndIf}
!macroend

!macro NSIS_HOOK_PREINSTALL
  !insertmacro VIDGRAB_CHECK --installer-prepare-install "$INSTDIR"
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  !insertmacro VIDGRAB_CHECK --installer-prepare-uninstall "$INSTDIR"
!macroend

!macro NSIS_HOOK_POSTINSTALL
  !insertmacro VIDGRAB_CHECK --installer-record "$INSTDIR"
!macroend
