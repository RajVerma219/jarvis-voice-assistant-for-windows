' Runs Jarvis hidden (no terminal). Logs to jarvis_background.log
Option Explicit
Dim fso, shell, dir, py, cmd, logf
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = dir
If fso.FileExists(dir & "\.venv312\Scripts\pythonw.exe") Then
  py = Chr(34) & dir & "\.venv312\Scripts\pythonw.exe" & Chr(34)
Else
  py = "pythonw.exe"
End If
shell.Environment("Process")("HF_HUB_DISABLE_SYMLINKS_WARNING") = "1"
If shell.Environment("Process")("JARVIS_WHISPER_DEVICE") = "" Then shell.Environment("Process")("JARVIS_WHISPER_DEVICE") = "cpu"
If shell.Environment("Process")("JARVIS_WHISPER_MODEL") = "" Then shell.Environment("Process")("JARVIS_WHISPER_MODEL") = "small"
If shell.Environment("Process")("JARVIS_DEBUG_AUDIO") = "" Then shell.Environment("Process")("JARVIS_DEBUG_AUDIO") = "0"
If shell.Environment("Process")("JARVIS_WAKE_MIN_RMS") = "" Then shell.Environment("Process")("JARVIS_WAKE_MIN_RMS") = "90"
If shell.Environment("Process")("JARVIS_DEVICE") = "" Then shell.Environment("Process")("JARVIS_DEVICE") = "48"
logf = Chr(34) & dir & "\jarvis_background.log" & Chr(34)
cmd = "cmd /c " & py & " " & Chr(34) & dir & "\jarvis.py" & Chr(34) & " >> " & logf & " 2>&1"
shell.Run cmd, 0, False
