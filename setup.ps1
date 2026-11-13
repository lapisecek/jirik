# Jiřík – instalace všeho potřebného od nuly (Python, Node.js, Ollama, knihovny, hlas, model, ikona).
# Spouští se přes Setup.bat. Dá se pustit kdykoli znovu – co už je hotové, přeskočí.
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$Root = $PSScriptRoot
Set-Location $Root
$Log = Join-Path $Root "data\logs\setup.log"
New-Item -ItemType Directory -Force (Split-Path $Log) | Out-Null
Start-Transcript -Path $Log -Append | Out-Null

function Step($n, $t) { Write-Host "`n  [$n/7] $t" -ForegroundColor White }
function Info($t) { Write-Host "        $t" -ForegroundColor Gray }
function Fail($t) { Write-Host "`n  ✗ $t`n  Podrobnosti: $Log`n" -ForegroundColor Red; Stop-Transcript | Out-Null; exit 1 }
function Refresh-Path {
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}
function Retry([scriptblock]$Block, [string]$What, [int]$Times = 3) {
  for ($i = 1; $i -le $Times; $i++) {
    $global:LASTEXITCODE = 0
    try { & $Block; if ($LASTEXITCODE -eq 0) { return } } catch { if ($i -eq $Times) { Fail "$What se nepovedlo: $_" } }
    if ($i -eq $Times) { Fail "$What se nepovedlo (je počítač připojený k internetu?)." }
    Info "Zkouším znovu ($What)…"; Start-Sleep 4
  }
}
function Have($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
$winget = Have winget
function Winget-Install($id, $what) {
  if (-not $winget) { Fail "Chybí $what a winget (Microsoft Store › „Instalační program aplikací“). Nainstalujte $what ručně a spusťte Setup znovu." }
  Info "Instaluji $what…"
  winget install -e --id $id --silent --accept-package-agreements --accept-source-agreements --disable-interactivity | Out-Null
  Refresh-Path
}

Write-Host "`n  Jiřík – instalace" -ForegroundColor White

# 1) Programy, které Jiřík potřebuje
Step 1 "Python, Node.js a Ollama"
$py = $null
foreach ($c in @(@("py", "-3"), @("python"))) {
  try {
    $exe = $c[0]; $rest = @($c | Select-Object -Skip 1)
    $v = & $exe @rest -c "import sys;print(sys.version_info>=(3,11), sys.executable)" 2>$null
    if ($v -and "$v".StartsWith("True")) { $py = "$v".Substring(5).Trim(); break }
  } catch { }
}
if (-not $py) {
  Winget-Install "Python.Python.3.12" "Python 3.12"
  $py = (Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe", "$env:ProgramFiles\Python3*\python.exe" -ErrorAction SilentlyContinue | Select-Object -First 1).FullName
  if (-not $py) { Fail "Python se nepodařilo nainstalovat." }
}
Info "Python: $py"
if (-not (Have npm)) { Winget-Install "OpenJS.NodeJS.LTS" "Node.js" }
if (-not (Have npm)) { Fail "Node.js se nepodařilo nainstalovat." }
if (-not (Have ollama)) { Winget-Install "Ollama.Ollama" "Ollama" }
if (-not (Have ollama)) {
  $o = Join-Path $env:LOCALAPPDATA "Programs\Ollama"
  if (Test-Path "$o\ollama.exe") { $env:Path += ";$o" } else { Fail "Ollamu se nepodařilo nainstalovat." }
}
$ov = ((ollama --version 2>$null) -join " ") -replace "[^0-9.]", " "
$ov = ($ov.Trim() -split "\s+" | Where-Object { $_ -match "^\d+\.\d+" } | Select-Object -Last 1)
if ($ov -and [version]$ov -lt [version]"0.30") {
  Info "Aktualizuji Ollamu ($ov → nejnovější, Qwen3.5 potřebuje novější)…"
  if ($winget) { winget upgrade -e --id Ollama.Ollama --silent --accept-package-agreements --accept-source-agreements | Out-Null }
}
# rychlejší modely: flash attention + menší paměť kontextu (víc vrstev se vejde do grafiky)
[Environment]::SetEnvironmentVariable("OLLAMA_FLASH_ATTENTION", "1", "User")
[Environment]::SetEnvironmentVariable("OLLAMA_KV_CACHE_TYPE", "q8_0", "User")
$env:OLLAMA_FLASH_ATTENTION = "1"; $env:OLLAMA_KV_CACHE_TYPE = "q8_0"

# 2) Python prostředí a knihovny
Step 2 "Knihovny (první instalace trvá pár minut)"
if (-not (Test-Path ".venv\Scripts\python.exe")) { & $py -m venv .venv; if ($LASTEXITCODE) { Fail "Prostředí Pythonu nejde vytvořit." } }
Retry { & .venv\Scripts\python.exe -m pip install --disable-pip-version-check --upgrade pip --quiet } "aktualizace pip"
Retry { & .venv\Scripts\python.exe -m pip install --disable-pip-version-check -e ".[pc]" --quiet } "instalace knihoven"

# 3) Hlas
Step 3 "Hlas Jirka"
$voice = "voices\cs_CZ-jirka-medium.onnx"
New-Item -ItemType Directory -Force voices | Out-Null
$base = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/cs/cs_CZ/jirka/medium"
foreach ($f in @("cs_CZ-jirka-medium.onnx", "cs_CZ-jirka-medium.onnx.json")) {
  $dst = Join-Path "voices" $f
  if (-not (Test-Path $dst) -or (Get-Item $dst).Length -lt 1000) {
    Retry { Invoke-WebRequest "$base/$f" -OutFile $dst -UseBasicParsing } "stažení hlasu"
  }
}

# 4) Aplikace
Step 4 "Aplikace (Electron)"
Retry { npm install --no-audit --no-fund --loglevel=error } "instalace aplikace"
if (-not (Test-Path "node_modules\electron\dist\electron.exe")) { Retry { node node_modules\electron\install.js } "stažení Electronu" }

# 5) Ollama běží?
Step 5 "Ollama"
$up = $false
try { Invoke-RestMethod "http://localhost:11434/api/version" -TimeoutSec 2 | Out-Null; $up = $true } catch { }
if (-not $up) {
  Start-Process ollama -ArgumentList "serve" -WindowStyle Hidden
  for ($i = 0; $i -lt 30 -and -not $up; $i++) {
    Start-Sleep 1
    try { Invoke-RestMethod "http://localhost:11434/api/version" -TimeoutSec 2 | Out-Null; $up = $true } catch { }
  }
}
if (-not $up) { Fail "Ollama se nespustila." }

# 6) Model pro úroveň Nízká (ostatní úrovně se stahují v aplikaci jedním kliknutím)
Step 6 "Model Qwen3.5 4B (3,4 GB)"
$have = (ollama list 2>$null) -join "`n"
if ($have -notmatch [regex]::Escape("qwen3.5:4b")) { Retry { ollama pull qwen3.5:4b } "stažení modelu" }

# 7) Ikona
Step 7 "Ikona na ploše"
& "$Root\tools\create-shortcut.ps1"

Write-Host "`n  ✓ Hotovo! Jiříka spustíte ikonou Jiřík na ploše.`n" -ForegroundColor Green
Stop-Transcript | Out-Null
