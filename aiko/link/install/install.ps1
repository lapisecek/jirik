# Jiřík Link – instalace na tento počítač. Spouští ji Jirik-Link-Setup.bat stažený z Jiříka.
param(
  [string]$PairUrl = "",
  [switch]$SkipElevation,
  [switch]$SkipAutostart
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; $OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$src = $PSScriptRoot
$dst = Join-Path $env:LOCALAPPDATA 'JirikLink'
$logDir = Join-Path $dst 'logs'
New-Item -ItemType Directory -Force $logDir | Out-Null
$logFile = Join-Path $logDir 'install.log'

function Write-Log([string]$Text) {
  try {
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Add-Content -LiteralPath $logFile -Value "[$ts] $Text" -Encoding UTF8
  } catch { }
}
function Step($t) { Write-Host "  › $t" -ForegroundColor Gray; Write-Log "STEP: $t" }
function Info($t) { Write-Host "    $t" -ForegroundColor DarkGray; Write-Log "INFO: $t" }
function Warn($t) { Write-Host "    ! $t" -ForegroundColor Yellow; Write-Log "WARN: $t" }
function Fail($t) {
  Write-Log "FAIL: $t"
  Write-Host "`n  ✗ $t" -ForegroundColor Red
  Write-Host "    Podrobný záznam: $logFile`n" -ForegroundColor DarkGray
  exit 1
}

function Invoke-Native {
  param(
    [Parameter(Mandatory = $true)][string]$FilePath,
    [string[]]$Arguments = @(),
    [switch]$LogOutput
  )
  $prevEap = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  $lines = @()
  $code = 0
  try {
    $out = & $FilePath @Arguments 2>&1
    $code = $LASTEXITCODE
    foreach ($item in $out) {
      $s = "$item"
      $lines += $s
      if ($LogOutput) { Write-Log "  | $s" }
    }
  } catch {
    $lines += "$($_.Exception.Message)"
    if ($LogOutput) { Write-Log "  | EXC: $($_.Exception.Message)" }
    if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { $code = $LASTEXITCODE } else { $code = 1 }
  } finally {
    $ErrorActionPreference = $prevEap
  }
  return [pscustomobject]@{ ExitCode = [int]$code; Output = $lines }
}

function Refresh-Path {
  $m = [Environment]::GetEnvironmentVariable('Path', 'Machine')
  $u = [Environment]::GetEnvironmentVariable('Path', 'User')
  $env:Path = @($m, $u) -join ';'
}

function Enable-Tls {
  try {
    $proto = [Net.SecurityProtocolType]::Tls12
    try { $proto = $proto -bor [Net.SecurityProtocolType]::Tls13 } catch { }
    [Net.ServicePointManager]::SecurityProtocol = $proto
  } catch { }
}

Write-Host "`n  Jiřík Link`n" -ForegroundColor White
Write-Log "=== Start install (PairUrl='$PairUrl', PS=$($PSVersionTable.PSVersion)) ==="
Enable-Tls

# 0) Prostředí: architektura, účet, volné místo
$arch = $env:PROCESSOR_ARCHITEW6432
if (-not $arch) { $arch = $env:PROCESSOR_ARCHITECTURE }
$wantBits = if ($arch -eq 'x86') { 32 } else { 64 }
$id = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$userSid = $id.User.Value
$userName = $id.Name
$isAdmin = ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
Write-Log "OS=$([Environment]::OSVersion.VersionString) Arch=$arch User=$userName SID=$userSid Admin=$isAdmin Dst=$dst"

if ($isAdmin) {
  try {
    $exp = Get-CimInstance Win32_Process -Filter "Name='explorer.exe'" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($exp) {
      $owner = Invoke-CimMethod -InputObject $exp -MethodName GetOwner -ErrorAction SilentlyContinue
      if ($owner -and $owner.User -and ($owner.User -ne $env:USERNAME)) {
        Warn "Instalátor běží jako správce pod jiným účtem ($userName) než plocha ($($owner.User)). Spusťte ho raději normálně bez „Spustit jako správce“."
      }
    }
  } catch { }
}

try {
  $rootDrive = Split-Path -Qualifier $dst
  $disk = Get-PSDrive -Name ($rootDrive.TrimEnd(':')) -ErrorAction SilentlyContinue
  if ($disk -and $disk.Free -lt 300MB) {
    Fail "Na disku $rootDrive zbývá málo místa ($([math]::Round($disk.Free/1MB)) MB) – je potřeba alespoň 300 MB."
  }
} catch { }

# 1) Python 3.11+ (všechna standardní místa, bez Windows Store zástupce, případně winget + přímý instalátor)
function Test-PythonCandidate {
  param([string]$Exe, [string[]]$PrefixArgs = @())
  if (-not $Exe) { return $null }
  $resolved = $Exe
  if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
    $cmd = Get-Command $Exe -ErrorAction SilentlyContinue
    if (-not $cmd) { return $null }
    $resolved = $cmd.Source
  }
  if (-not $resolved -or $resolved -match '\\Microsoft\\WindowsApps\\') { return $null }
  try {
    if ((Get-Item -LiteralPath $resolved -ErrorAction Stop).Length -le 0) { return $null }
  } catch { return $null }

  $probe = 'import sys, venv, ensurepip, ssl, ctypes, struct; print(str(sys.version_info[0])+chr(124)+str(sys.version_info[1])+chr(124)+str(struct.calcsize(chr(80))*8)+chr(124)+sys.executable)'
  $r = Invoke-Native -FilePath $resolved -Arguments (@($PrefixArgs) + @('-c', $probe))
  if ($r.ExitCode -ne 0) { return $null }
  $line = $r.Output | Where-Object { $_ -match '^\d+\|\d+\|\d+\|' } | Select-Object -First 1
  if (-not $line) { return $null }
  $parts = $line.Split('|', 4)
  $maj = [int]$parts[0]; $min = [int]$parts[1]; $bits = [int]$parts[2]; $realExe = $parts[3].Trim()
  if ($maj -ne 3 -or $min -lt 11 -or $min -gt 15) { return $null }
  if (-not (Test-Path -LiteralPath $realExe -PathType Leaf)) { return $null }

  # Preferujeme architekturu systému a stabilní verze s hotovými wheel balíčky (3.12 > 3.13 > 3.11 > 3.14)
  $archScore = if ($bits -eq $wantBits) { 1000 } else { 0 }
  $verScore = switch ($min) { 12 { 50 } 13 { 45 } 11 { 40 } 14 { 30 } default { 10 } }
  return [pscustomobject]@{ Path = $realExe; Major = $maj; Minor = $min; Bits = $bits; Score = ($archScore + $verScore) }
}

function Find-Python {
  $candidates = New-Object System.Collections.Generic.List[object]
  $seen = New-Object System.Collections.Generic.HashSet[string]([StringComparer]::OrdinalIgnoreCase)

  $addPath = {
    param([string]$Path, [string[]]$Args = @())
    $res = Test-PythonCandidate -Exe $Path -PrefixArgs $Args
    if ($res -and $seen.Add($res.Path)) {
      Write-Log "Python OK: $($res.Path) ($($res.Major).$($res.Minor) $($res.Bits)-bit, score=$($res.Score))"
      $candidates.Add($res)
    }
  }

  # a) py launcher
  $pyCmd = Get-Command 'py' -ErrorAction SilentlyContinue
  if ($pyCmd) {
    foreach ($ver in @('-3.12', '-3.13', '-3.11', '-3.14', '-3')) {
      & $addPath $pyCmd.Source @($ver)
    }
    $pyList = Invoke-Native -FilePath $pyCmd.Source -Arguments @('-0p')
    foreach ($ln in $pyList.Output) {
      if ($ln -match '([A-Za-z]:\\[^\r\n]+python\.exe)') { & $addPath $matches[1] }
    }
  }

  # b) PATH
  foreach ($cmdName in @('python', 'python3')) {
    & $addPath $cmdName
  }

  # c) Registr Windows (PythonCore)
  foreach ($hive in @(
    'HKCU:\Software\Python\PythonCore',
    'HKLM:\Software\Python\PythonCore',
    'HKLM:\Software\WOW6432Node\Python\PythonCore'
  )) {
    Get-ChildItem $hive -ErrorAction SilentlyContinue | ForEach-Object {
      $ip = Join-Path $_.PSPath 'InstallPath'
      if (Test-Path $ip) {
        $props = Get-ItemProperty $ip -ErrorAction SilentlyContinue
        if ($props.ExecutablePath) { & $addPath $props.ExecutablePath }
        elseif ($props.'(default)') { & $addPath (Join-Path $props.'(default)' 'python.exe') }
      }
    }
  }

  # d) Známé složky na disku (včetně nového Python Install Manageru pythoncore-* i pyenv/scoop/uv)
  $globs = @(
    "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
    "$env:LOCALAPPDATA\Python\pythoncore-*\python.exe",
    "$env:ProgramFiles\Python3*\python.exe",
    "${env:ProgramFiles(x86)}\Python3*\python.exe",
    'C:\Python3*\python.exe',
    "$env:USERPROFILE\.pyenv\pyenv-win\versions\*\python.exe",
    "$env:USERPROFILE\scoop\apps\python*\current\python.exe",
    "$env:LOCALAPPDATA\uv\python\*\python.exe"
  )
  foreach ($g in $globs) {
    Get-Item -Path $g -ErrorAction SilentlyContinue | ForEach-Object { & $addPath $_.FullName }
  }

  $best = $candidates | Sort-Object Score -Descending | Select-Object -First 1
  if ($best) { return $best.Path }
  return $null
}

function Download-File([string]$Url, [string]$OutFile) {
  for ($i = 1; $i -le 3; $i++) {
    try {
      Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing -TimeoutSec 60
      if ((Test-Path -LiteralPath $OutFile) -and (Get-Item -LiteralPath $OutFile).Length -gt 10MB) { return $true }
    } catch {
      Write-Log "Download attempt $i failed (Invoke-WebRequest): $($_.Exception.Message)"
    }
    try {
      $wc = New-Object System.Net.WebClient
      $wc.DownloadFile($Url, $OutFile)
      if ((Test-Path -LiteralPath $OutFile) -and (Get-Item -LiteralPath $OutFile).Length -gt 10MB) { return $true }
    } catch {
      Write-Log "Download attempt $i failed (WebClient): $($_.Exception.Message)"
    }
    Start-Sleep -Seconds (2 * $i)
  }
  return $false
}

Step 'Hledám Python 3.11+'
$py = Find-Python
if (-not $py) {
  Step 'Instaluji Python 3.12 (může to chvíli trvat)'
  $wg = Get-Command winget -ErrorAction SilentlyContinue
  if ($wg) {
    Info 'Zkouším instalaci přes winget…'
    $r = Invoke-Native -FilePath $wg.Source -Arguments @(
      'install', '-e', '--id', 'Python.Python.3.12', '--source', 'winget',
      '--scope', 'user', '--silent', '--accept-package-agreements', '--accept-source-agreements', '--disable-interactivity'
    ) -LogOutput
    Refresh-Path
    $py = Find-Python
  }
  if (-not $py) {
    $file = switch ($arch) {
      'ARM64' { 'python-3.12.10-arm64.exe' }
      'x86'   { 'python-3.12.10.exe' }
      default { 'python-3.12.10-amd64.exe' }
    }
    $url = "https://www.python.org/ftp/python/3.12.10/$file"
    $inst = Join-Path $env:TEMP $file
    Info "Stahuji oficiální instalátor Python 3.12 ($arch)…"
    if (-not (Download-File $url $inst)) {
      Fail 'Nepodařilo se stáhnout instalátor Pythonu (zkontrolujte připojení k internetu).'
    }
    $sig = Get-AuthenticodeSignature -FilePath $inst -ErrorAction SilentlyContinue
    if ($sig -and $sig.Status -ne 'Valid' -and $sig.SignerCertificate -and $sig.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
      Remove-Item -LiteralPath $inst -Force -ErrorAction SilentlyContinue
      Fail 'Stažený instalátor Pythonu nemá platný podpis Python Software Foundation.'
    }
    Info 'Spouštím tichou instalaci Pythonu…'
    $proc = Start-Process -FilePath $inst -ArgumentList '/quiet', 'InstallAllUsers=0', 'PrependPath=1', 'Include_launcher=1', 'Include_pip=1', 'Include_test=0' -Wait -PassThru
    Remove-Item -LiteralPath $inst -Force -ErrorAction SilentlyContinue
    Write-Log "python.org installer exit=$($proc.ExitCode)"
    Refresh-Path
    $py = Find-Python
  }
  if (-not $py) { Fail 'Python 3.11+ se nepodařilo nainstalovat. Nainstalujte Python 3.12 z python.org a spusťte instalaci znovu.' }
}
Info "Používám Python: $py"

# 2) Zastavit starou verzi, počkat na uvolnění souborů a zkopírovat kód
Step 'Kopíruji soubory'
if (-not $SkipAutostart) {
  try { Get-ScheduledTask -TaskName 'Jirik Link' -ErrorAction SilentlyContinue | Stop-ScheduledTask -ErrorAction SilentlyContinue } catch { }
}
$stopTargets = {
  Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue | Where-Object {
    ($_.CommandLine -match 'aiko\.(link\.node|agents\.pc)') -and
    (($_.ExecutablePath -and $_.ExecutablePath.StartsWith($dst, [StringComparison]::OrdinalIgnoreCase)) -or ($_.CommandLine -match 'JirikLink'))
  }
}
& $stopTargets | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
for ($w = 0; $w -lt 20; $w++) {
  if (-not (& $stopTargets)) { break }
  Start-Sleep -Milliseconds 300
}

New-Item -ItemType Directory -Force $dst | Out-Null
$linkJson = Join-Path $dst 'link.json'
$linkBak = Join-Path $dst 'link.json.bak'
if (Test-Path -LiteralPath $linkJson) { Copy-Item -LiteralPath $linkJson -Destination $linkBak -Force -ErrorAction SilentlyContinue }
if (Test-Path -LiteralPath "$dst\aiko") { Remove-Item -LiteralPath "$dst\aiko" -Recurse -Force }
Copy-Item -LiteralPath "$src\aiko" -Destination "$dst\aiko" -Recurse -Force
foreach ($f in @('uninstall.ps1', 'Uninstall Jirik Link.bat', 'requirements.txt', 'requirements-ocr.txt')) {
  if (Test-Path -LiteralPath (Join-Path $src $f)) {
    Copy-Item -LiteralPath (Join-Path $src $f) -Destination $dst -Force
  }
}

# 3) Prostředí .venv, instalace knihoven a ověření importů
Step 'Připravuji prostředí (první instalace trvá pár minut)'
$venvDir = Join-Path $dst '.venv'
$vpy = Join-Path $venvDir 'Scripts\python.exe'
$vpyw = Join-Path $venvDir 'Scripts\pythonw.exe'

$venvOk = $false
if ((Test-Path -LiteralPath $vpy) -and (Test-Path -LiteralPath $vpyw)) {
  $chk = Invoke-Native -FilePath $vpy -Arguments @('-c', 'import sys, pip; assert sys.version_info >= (3, 11)')
  if ($chk.ExitCode -eq 0) { $venvOk = $true }
}
if (-not $venvOk) {
  if (Test-Path -LiteralPath $venvDir) {
    Info 'Obnovuji neúplné prostředí .venv…'
    Remove-Item -LiteralPath $venvDir -Recurse -Force -ErrorAction SilentlyContinue
  }
  $mk = Invoke-Native -FilePath $py -Arguments @('-m', 'venv', $venvDir) -LogOutput
  if ($mk.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $vpy)) {
    Fail 'Vytvoření Python prostředí (.venv) selhalo.'
  }
}

# Zajistíme funkční pip (bez selhání na upozorněních na stderr)
$pipChk = Invoke-Native -FilePath $vpy -Arguments @('-m', 'pip', '--version')
if ($pipChk.ExitCode -ne 0) {
  Invoke-Native -FilePath $vpy -Arguments @('-m', 'ensurepip', '--upgrade') -LogOutput | Out-Null
}
Invoke-Native -FilePath $vpy -Arguments @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', '--upgrade', 'pip') -LogOutput | Out-Null

$coreInstalled = $false
for ($i = 1; $i -le 3; $i++) {
  $pipArgs = @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', '--no-warn-script-location', '--prefer-binary')
  if ($i -eq 3) {
    Info 'Zkouším čistou instalaci knihoven bez cache…'
    $pipArgs += '--no-cache-dir'
  }
  $pipArgs += @('-r', (Join-Path $dst 'requirements.txt'))
  $res = Invoke-Native -FilePath $vpy -Arguments $pipArgs -LogOutput
  if ($res.ExitCode -eq 0) { $coreInstalled = $true; break }
  Warn "Pokus $i/3 instalace knihoven skončil chybou (kód $($res.ExitCode)), zkouším znovu…"
  Start-Sleep -Seconds 3
}
if (-not $coreInstalled) {
  Fail 'Instalace základních knihoven selhala (zkontrolujte připojení k internetu a záznam install.log).'
}

if (Test-Path -LiteralPath (Join-Path $dst 'requirements-ocr.txt')) {
  Info 'Doinstalovávám volitelné Windows OCR…'
  $ocrRes = Invoke-Native -FilePath $vpy -Arguments @(
    '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', '--no-warn-script-location',
    '--prefer-binary', '-r', (Join-Path $dst 'requirements-ocr.txt')
  ) -LogOutput
  if ($ocrRes.ExitCode -ne 0) {
    Warn 'Volitelné Windows OCR balíčky se nepodařilo nainstalovat – ovládání počítače bude fungovat i bez OCR.'
  }
}

$imp = Invoke-Native -FilePath $vpy -Arguments @(
  '-c', 'import websockets, cryptography, PIL, mss, pyautogui, pywinauto, aiko.link.node, aiko.agents.pc; print(True)'
) -LogOutput
if ($imp.ExitCode -ne 0) {
  Fail 'Kontrola nainstalovaných Python modulů selhala (viz install.log).'
}

# 4) Spárování: oba počítače ukážou stejný kód
if ($PairUrl) {
  Step 'Páruji s Jiříkem'
  $env:PYTHONUTF8 = '1'
  Push-Location $dst
  $prevEap = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  & $vpy -m aiko.link.pair --url $PairUrl
  $pairCode = $LASTEXITCODE
  $ErrorActionPreference = $prevEap
  Pop-Location
  if ($pairCode -ne 0) {
    if ((Test-Path -LiteralPath $linkBak) -and -not (Test-Path -LiteralPath $linkJson)) {
      Copy-Item -LiteralPath $linkBak -Destination $linkJson -Force -ErrorAction SilentlyContinue
    }
    Fail 'Spárování se nepovedlo. Otevřete v Jiříkovi „Přidat zařízení“ a spusťte instalátor znovu.'
  }
  Remove-Item -LiteralPath $linkBak -Force -ErrorAction SilentlyContinue
} elseif (-not (Test-Path -LiteralPath $linkJson)) {
  Fail 'Chybí párovací adresa. Instalátor stáhněte z Jiříka (Zařízení › Přidat zařízení).'
}

$aclRes = Invoke-Native -FilePath 'icacls.exe' -Arguments @($linkJson, '/inheritance:r', '/grant:r', "*${userSid}:(R,W)") -LogOutput
if ($aclRes.ExitCode -ne 0) {
  Warn 'Omezení přístupových práv k link.json přes icacls se nepovedlo (pravděpodobně ne-NTFS disk).'
}

# 5) Firewall, profil sítě a Wake-on-LAN (jediné okno UAC)
Step 'Povoluji probouzení z místní sítě'
$conf = Get-Content -LiteralPath $linkJson -Raw -Encoding UTF8 | ConvertFrom-Json
$port = [int]$conf.udp_port

$pubProfiles = @()
try {
  $pubProfiles = @(Get-NetConnectionProfile -ErrorAction SilentlyContinue | Where-Object { $_.NetworkCategory -eq 'Public' })
} catch { }

$switchToPrivate = $false
if ($pubProfiles.Count -gt 0) {
  $names = ($pubProfiles | ForEach-Object { $_.Name }) -join ', '
  if (-not $env:AIKO_NONINTERACTIVE) {
    $ans = Read-Host "    Síť ($names) je nastavená jako „Veřejná“. Přepnout ji na „Soukromou“, aby se počítač dal probudit? [A/n]"
    if (-not $ans -or $ans -match '^(a|ano|y|yes)$') { $switchToPrivate = $true }
  }
}

$privCmd = ""
if ($switchToPrivate -and $pubProfiles.Count -gt 0) {
  $idxs = ($pubProfiles | ForEach-Object { [int]$_.InterfaceIndex }) -join ','
  $privCmd = "foreach (`$idx in @($idxs)) { Set-NetConnectionProfile -InterfaceIndex `$idx -NetworkCategory Private -ErrorAction SilentlyContinue }`n"
}

$admin = @"
`$ErrorActionPreference = 'Continue'
${privCmd}Remove-NetFirewallRule -DisplayName 'Jirik Link' -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName 'Jirik Link' -Direction Inbound -Protocol UDP -LocalPort $port -RemoteAddress LocalSubnet -Profile Private,Domain -Action Allow | Out-Null
Get-NetAdapter -Physical -ErrorAction SilentlyContinue | Where-Object Status -eq 'Up' | ForEach-Object {
  Set-NetAdapterPowerManagement -Name `$_.Name -WakeOnMagicPacket Enabled -ErrorAction SilentlyContinue
  powercfg /deviceenablewake "`$(`$_.InterfaceDescription)" 2>`$null | Out-Null
}
"@
$fwOk = $false
if (-not $SkipElevation) {
  $tmpAdmin = Join-Path $env:TEMP 'jirik-link-admin.ps1'
  Set-Content -LiteralPath $tmpAdmin $admin -Encoding UTF8
  try {
    $pAdmin = Start-Process powershell -Verb RunAs -Wait -PassThru -WindowStyle Hidden -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $tmpAdmin
    $fwOk = ($pAdmin.ExitCode -eq 0)
  } catch {
    Write-Log "UAC admin step skipped/failed: $($_.Exception.Message)"
  }
  Remove-Item -LiteralPath $tmpAdmin -Force -ErrorAction SilentlyContinue
  if (-not $fwOk) {
    Warn 'Firewall / Wake-on-LAN se nepodařilo nastavit (UAC odmítnuto). Jiřík přejde na záložní odchozí TLS spojení.'
  }
}

# 6) Hlídač při každém přihlášení (Task Scheduler + záloha po přihlášení) a živé ověření
Step 'Nastavuji automatické spuštění'
$taskRegistered = $false
if (-not $SkipAutostart) {
  try {
    $action = New-ScheduledTaskAction -Execute $vpyw -Argument '-m aiko.link.node' -WorkingDirectory $dst
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $userName
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
      -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -Priority 7
    $principal = New-ScheduledTaskPrincipal -UserId $userName -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName 'Jirik Link' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
    Start-ScheduledTask -TaskName 'Jirik Link'
    $taskRegistered = $true
  } catch {
    Write-Log "ScheduledTask failed: $($_.Exception.Message)"
    Warn 'Plánovač úloh nebyl povolen – nastavuji spuštění přes složku Po spuštění.'
    try {
      $startup = [Environment]::GetFolderPath('Startup')
      $lnk = Join-Path $startup 'Jirik Link.lnk'
      $sh = New-Object -ComObject WScript.Shell
      $sc = $sh.CreateShortcut($lnk)
      $sc.TargetPath = $vpyw
      $sc.Arguments = '-m aiko.link.node'
      $sc.WorkingDirectory = $dst
      $sc.WindowStyle = 7
      $sc.Save()
      $taskRegistered = $true
    } catch {
      Write-Log "Startup shortcut failed: $($_.Exception.Message)"
    }
  }
}

Start-Sleep -Seconds 2
$nodeProcQuery = {
  Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue | Where-Object {
    ($_.CommandLine -match 'aiko\.link\.node') -and ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($dst, [StringComparison]::OrdinalIgnoreCase))
  }
}
$runningNode = & $nodeProcQuery
if (-not $runningNode) {
  Info 'Spouštím hlídače přímo…'
  Start-Process -FilePath $vpyw -ArgumentList '-m', 'aiko.link.node' -WorkingDirectory $dst -WindowStyle Hidden
  Start-Sleep -Seconds 2
  $runningNode = & $nodeProcQuery
}
if (-not $runningNode) {
  $nodeLog = Join-Path $logDir 'node.log'
  if (Test-Path -LiteralPath $nodeLog) { Get-Content -LiteralPath $nodeLog -Tail 15 | ForEach-Object { Write-Log "node.log: $_" } }
  Fail 'Hlídač (aiko.link.node) se nepodařilo udržet spuštěný. Podrobnosti jsou v logs\install.log a logs\node.log.'
}

# Ověříme spojení zpět na TLS bránu hlavního počítače
$mainHosts = @($conf.hosts)
$tlsPort = [int]$conf.port
if ($mainHosts.Count -gt 0) {
  $reachAny = $false
  foreach ($mh in $mainHosts) {
    try {
      $tc = New-Object System.Net.Sockets.TcpClient
      $ar = $tc.BeginConnect($mh, $tlsPort, $null, $null)
      if ($ar.AsyncWaitHandle.WaitOne(3000, $false) -and $tc.Connected) { $reachAny = $true }
      $tc.Close()
    } catch { }
  }
  if (-not $reachAny) {
    Warn "Port $tlsPort hlavního počítače ($($mainHosts -join ', ')) zatím neodpovídá – povolte na hlavním počítači okno firewallu pro Jiřík Link."
  } else {
    Info "Spojení s hlavním počítačem (port $tlsPort) ověřeno."
  }
}

Write-Log "=== Install DONE (node running, taskRegistered=$taskRegistered) ==="
Write-Host "`n  ✓ Hotovo. Počítač „$($conf.name)“ je připojený a Jiřík ho najde sám.`n" -ForegroundColor Green
