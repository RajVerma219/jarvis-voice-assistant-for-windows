# Stops Jarvis pythonw/python processes launched from this folder (best-effort).
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$jarvis = Join-Path $dir 'jarvis.py'
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" | ForEach-Object {
  $cmd = $_.CommandLine
  if ($cmd -and $cmd -like "*$jarvis*") {
    try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; Write-Output "Stopped PID $($_.ProcessId)" } catch { Write-Output "Failed PID $($_.ProcessId): $($_.Exception.Message)" }
  }
}
