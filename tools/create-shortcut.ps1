# Vytvoří na ploše ikonu „Jiřík“, která spustí aplikaci (Electron) z této složky.
$Root = Split-Path -Parent $PSScriptRoot
$Electron = Join-Path $Root "node_modules\electron\dist\electron.exe"
if (-not (Test-Path $Electron)) { throw "Chybí Electron – spusťte nejdřív Setup.bat." }
$Desktop = [Environment]::GetFolderPath("Desktop")
$Old = Join-Path $Desktop "Aiko.lnk"; if (Test-Path $Old) { Remove-Item $Old }
$Final = Join-Path $Desktop "Jiřík.lnk"
Get-ChildItem $Desktop -Filter "Jir*k.lnk" -ErrorAction SilentlyContinue | Remove-Item
$Link = Join-Path $Desktop "Jirik-tmp.lnk"   # WScript.Shell neumí „ř“ – jméno se opraví až po uložení
$Shell = New-Object -ComObject WScript.Shell
$S = $Shell.CreateShortcut($Link)
$S.TargetPath = $Electron
$S.Arguments = "`"$Root`""
$S.WorkingDirectory = $Root
$S.IconLocation = (Join-Path $Root "app\assets\jirik.ico") + ",0"
$S.Description = "Jiřík – místní AI chat, který ovládá počítač"
$S.Save()
Move-Item -LiteralPath $Link -Destination $Final -Force
$Link = $Final
Write-Host "  Ikona: $Link"
