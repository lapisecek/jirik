# Aiko – instalace všeho potřebného a ikona na ploše. Spouští se přes Setup.bat (opakovaně bez obav).
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location $Root

function Step($t) { Write-Host "`n=== $t ===" -ForegroundColor Cyan }

Step "1/5 Python prostředí a knihovny"
if (-not (Test-Path ".venv\Scripts\python.exe")) { py -3 -m venv .venv }
& .venv\Scripts\python.exe -m pip install --upgrade pip --quiet
& .venv\Scripts\python.exe -m pip install -e ".[pc]" piper-tts numpy --quiet

Step "2/5 Hlas Jirka"
$voice = "voices\cs_CZ-jirka-medium.onnx"
New-Item -ItemType Directory -Force voices | Out-Null
$base = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/cs/cs_CZ/jirka/medium"
if (-not (Test-Path $voice)) { curl.exe -fL -o $voice "$base/cs_CZ-jirka-medium.onnx" }
if (-not (Test-Path "$voice.json")) { curl.exe -fL -o "$voice.json" "$base/cs_CZ-jirka-medium.onnx.json" }

Step "3/5 Aplikace (Electron)"
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { throw "Chybí Node.js – nainstalujte ho z https://nodejs.org a spusťte Setup znovu." }
npm install --no-audit --no-fund
if (-not (Test-Path "node_modules\electron\dist\electron.exe")) { node node_modules\electron\install.js }

Step "4/5 Model pro Ollamu"
$model = "hf.co/unsloth/gemma-3-4b-it-GGUF:Q4_K_M"
if (Get-Command ollama -ErrorAction SilentlyContinue) {
    $have = (ollama list) -join "`n"
    if ($have -notmatch [regex]::Escape($model)) { ollama pull $model }
} else {
    Write-Host "  Ollama není nainstalovaná (https://ollama.com) – bez ní Aiko rozumí jen jednoduchým větám." -ForegroundColor Yellow
}

Step "5/5 Ikona na ploše"
& "$Root\tools\create-shortcut.ps1"

Write-Host "`nHotovo! Aiko spustíte ikonou Aiko na ploše." -ForegroundColor Green
