# Creates a Startup shortcut so Jarvis runs hidden at sign-in.
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$vbs = Join-Path $dir 'run_jarvis_background.vbs'
$startup = [Environment]::GetFolderPath('Startup')
$lnk = Join-Path $startup 'Jarvis Assistant.lnk'
$w = New-Object -ComObject WScript.Shell
$s = $w.CreateShortcut($lnk)
$s.TargetPath = 'wscript.exe'
$s.Arguments = '"' + $vbs + '"'
$s.WorkingDirectory = $dir
$s.WindowStyle = 7
$s.Description = 'Run Jarvis assistant in background'
$s.Save()
Write-Output "Created startup shortcut: $lnk"
