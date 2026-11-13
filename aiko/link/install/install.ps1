# Jiřík Link – instalace na tento počítač. Spouští ji Jirik-Link-Setup.bat stažený z Jiříka.
param([string]$PairUrl = "")
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$src = $PSScriptRoot
$dst = Join-Path $env:LOCALAPPDATA 'JirikLink'

function Step($t) { Write-Host "  › $t" -ForegroundColor Gray }
function Fail($t) { Write-Host "`n  ✗ $t`n" -ForegroundColor Red; exit 1 }
Write-Host "`n  Jiřík Link`n" -ForegroundColor White

# 1) Python 3.11+ (když chybí, doinstaluje se pro tohoto uživatele – bez práv správce)
function Find-Python {
  foreach ($c in @(@('py', '-3'), @('python'))) {
    try {
      $exe = $c[0]; $rest = @($c | Select-Object -Skip 1)
      $v = & $exe @rest -c "import sys;print(sys.version_info>=(3,11), sys.executable)" 2>$null
      if ($v -and "$v".StartsWith('True')) { return "$v".Substring(5).Trim() }
    } catch { }
  }
  foreach ($p in Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue | Sort-Object FullName -Descending) {
    return $p.FullName
  }
  return $null
}
Step 'Hledám Python'
$py = Find-Python
if (-not $py) {
  Step 'Instaluji Python 3.12 (může to chvíli trvat)'
  if (Get-Command winget -ErrorAction SilentlyContinue) {
    winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements | Out-Null
  } else {
    $inst = Join-Path $env:TEMP 'python-setup.exe'
    Invoke-WebRequest 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe' -OutFile $inst -UseBasicParsing
    Start-Process $inst -ArgumentList '/quiet', 'InstallAllUsers=0', 'PrependPath=1', 'Include_launcher=1' -Wait
  }
  $py = Find-Python
  if (-not $py) { Fail 'Python se nepodařilo nainstalovat. Nainstalujte ho z python.org a spusťte instalaci znovu.' }
}

# 2) Zastavit starou verzi a zkopírovat soubory (klíč zařízení z dřívějška zůstává)
Step 'Kopíruji soubory'
Get-ScheduledTask -TaskName 'Jirik Link' -ErrorAction SilentlyContinue | Stop-ScheduledTask -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -match 'aiko\.(link\.node|agents\.pc)' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
New-Item -ItemType Directory -Force $dst | Out-Null
if (Test-Path "$dst\aiko") { Remove-Item "$dst\aiko" -Recurse -Force }
Copy-Item "$src\aiko" "$dst\aiko" -Recurse -Force
Copy-Item "$src\uninstall.ps1", "$src\Uninstall Jirik Link.bat", "$src\requirements.txt" $dst -Force

# 3) Prostředí a knihovny
Step 'Připravuji prostředí (první instalace trvá pár minut)'
if (-not (Test-Path "$dst\.venv\Scripts\python.exe")) { & $py -m venv "$dst\.venv" }
$vpy = "$dst\.venv\Scripts\python.exe"
& $vpy -m pip install --disable-pip-version-check -q --upgrade pip 2>$null | Out-Null
for ($i = 1; $i -le 3; $i++) {
  & $vpy -m pip install --disable-pip-version-check -q -r "$dst\requirements.txt"
  if ($LASTEXITCODE -eq 0) { break }
  if ($i -eq 3) { Fail 'Instalace knihoven selhala (je počítač připojený k internetu?).' }
  Start-Sleep 3
}

# 4) Spárování: oba počítače ukážou stejný kód
if ($PairUrl) {
  Step 'Páruji s Jiříkem'
  $env:PYTHONUTF8 = '1'
  Push-Location $dst
  & $vpy -m aiko.link.pair --url $PairUrl
  $code = $LASTEXITCODE
  Pop-Location
  if ($code -ne 0) { Fail 'Spárování se nepovedlo. Otevřete v Jiříkovi „Přidat zařízení“ a spusťte instalátor znovu.' }
} elseif (-not (Test-Path "$dst\link.json")) {
  Fail 'Chybí párovací adresa. Instalátor stáhněte z Jiříka (Zařízení › Přidat zařízení).'
}
icacls "$dst\link.json" /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null

# 5) Firewall a Wake-on-LAN (jediné okno UAC): probouzení jen z místní sítě
Step 'Povoluji probouzení z místní sítě'
$conf = Get-Content "$dst\link.json" -Raw | ConvertFrom-Json
$port = [int]$conf.udp_port
$admin = @"
Remove-NetFirewallRule -DisplayName 'Jirik Link' -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName 'Jirik Link' -Direction Inbound -Protocol UDP -LocalPort $port -RemoteAddress LocalSubnet -Profile Private,Domain -Action Allow | Out-Null
Get-NetAdapter -Physical -ErrorAction SilentlyContinue | Where-Object Status -eq 'Up' | ForEach-Object {
  Set-NetAdapterPowerManagement -Name `$_.Name -WakeOnMagicPacket Enabled -ErrorAction SilentlyContinue
  powercfg /deviceenablewake "`$(`$_.InterfaceDescription)" 2>`$null | Out-Null
}
"@
$tmp = Join-Path $env:TEMP 'jirik-link-admin.ps1'
Set-Content $tmp $admin -Encoding UTF8
try { Start-Process powershell -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $tmp }
catch { Write-Host '    Bez povolení ve firewallu se tento počítač nedá probudit na dálku.' -ForegroundColor Yellow }
Remove-Item $tmp -ErrorAction SilentlyContinue
$netCat = (Get-NetConnectionProfile -ErrorAction SilentlyContinue | Select-Object -First 1).NetworkCategory
if ($netCat -eq 'Public') {
  Write-Host '    Síť je nastavená jako „Veřejná“ – přepněte ji na „Soukromá“ (Nastavení › Síť), jinak se Jiřík nedovolá.' -ForegroundColor Yellow
}

# 6) Hlídač při každém přihlášení (jako tento uživatel – jen tak může ovládat obrazovku)
Step 'Nastavuji automatické spuštění'
$action = New-ScheduledTaskAction -Execute "$dst\.venv\Scripts\pythonw.exe" -Argument '-m aiko.link.node' -WorkingDirectory $dst
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -Priority 7
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'Jirik Link' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName 'Jirik Link'

Write-Host "`n  ✓ Hotovo. Počítač „$($conf.name)“ teď Jiřík najde sám.`n" -ForegroundColor Green
