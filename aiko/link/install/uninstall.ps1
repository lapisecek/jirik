# Jiřík Link – odinstalace z tohoto počítače
$ErrorActionPreference = 'SilentlyContinue'
$dst = Join-Path $env:LOCALAPPDATA 'JirikLink'
Unregister-ScheduledTask -TaskName 'Jirik Link' -Confirm:$false
$startupLnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'Jirik Link.lnk'
if (Test-Path -LiteralPath $startupLnk) { Remove-Item -LiteralPath $startupLnk -Force }
Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'Jirik Link' -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -match 'aiko\.(link\.node|agents\.pc)' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Process powershell -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList '-NoProfile', '-Command', "Remove-NetFirewallRule -DisplayName 'Jirik Link'"
Set-Location $env:TEMP
Remove-Item $dst -Recurse -Force
Write-Host "`n  Jiřík Link je odinstalovaný.`n" -ForegroundColor Green
