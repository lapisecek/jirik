# Vytvoří čistý zip pro GitHub Release z trackovaných souborů v repozitáři (bez data/, .venv/, node_modules/ a osobního nastavení).
param(
  [string]$OutDir = "dist"
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$pkg = Get-Content "package.json" -Raw | ConvertFrom-Json
$ver = $pkg.version
if (-not $ver) { throw "Chybí verze v package.json" }

$files = @(git ls-files) | Where-Object {
  $_ -and
  $_ -notmatch '^\.github/' -and
  $_ -notmatch '^tests/' -and
  $_ -notin @('.gitignore', '.gitattributes')
}
if (-not $files) { throw "Žádné soubory z git ls-files." }

New-Item -ItemType Directory -Force $OutDir | Out-Null
$zipPath = Join-Path $Root "$OutDir\Jirik-v$ver-win.zip"
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::Open($zipPath, [System.IO.Compression.ZipArchiveMode]::Create)
try {
  foreach ($rel in $files) {
    $full = Join-Path $Root $rel
    if (-not (Test-Path $full)) { continue }
    $entryName = $rel -replace '\\', '/'
    [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
      $zip, $full, $entryName, [System.IO.Compression.CompressionLevel]::Optimal
    ) | Out-Null
  }
} finally {
  $zip.Dispose()
}

$sizeKB = [math]::Round((Get-Item $zipPath).Length / 1KB, 1)
Write-Host "Vytvořeno: $zipPath ($sizeKB KB, $($files.Count) souborů)"
