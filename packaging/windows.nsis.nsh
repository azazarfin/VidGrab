!macro VIDGRAB_PREPARE_FILES
  !define VidGrabPrepareId ${__LINE__}
  IfFileExists "$INSTDIR\${MAINBINARYNAME}.exe" 0 vidgrab_prepare_done_${VidGrabPrepareId}
  nsExec::ExecToStack /TIMEOUT=30000 '"$INSTDIR\${MAINBINARYNAME}.exe" --prepare-uninstall'
  Pop $0
  Pop $1
  ${If} $0 != 0
    IfSilent +2 0
    MessageBox MB_OK|MB_ICONEXCLAMATION "Finish active downloads and quit VidGrab before changing its installed files.$\r$\n$1"
    SetErrorLevel 2
    Abort
  ${EndIf}
  vidgrab_prepare_done_${VidGrabPrepareId}:
  !undef VidGrabPrepareId
!macroend

!macro NSIS_HOOK_PREINSTALL
  !insertmacro VIDGRAB_PREPARE_FILES
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  !insertmacro VIDGRAB_PREPARE_FILES
!macroend
