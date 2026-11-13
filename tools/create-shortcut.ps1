# Vytvoří na ploše ikonu „Aiko“, která spustí aplikaci (Electron) z této složky.
$Root = Split-Path -Parent $PSScriptRoot
$Electron = Join-Path $Root "node_modules\electron\dist\electron.exe"
if (-not (Test-Path $Electron)) { throw "Chybí Electron – spusťte nejdřív Setup.bat." }
$Desktop = [Environment]::GetFolderPath("Desktop")
$Link = Join-Path $Desktop "Aiko.lnk"
$Shell = New-Object -ComObject WScript.Shell
$S = $Shell.CreateShortcut($Link)
$S.TargetPath = $Electron
$S.Arguments = "`"$Root`""
$S.WorkingDirectory = $Root
$S.IconLocation = (Join-Path $Root "app\assets\aiko.ico") + ",0"
$S.Description = "Aiko – místní AI pomocnice s hlasem Jirka"
$S.Save()
Write-Host "  Ikona: $Link"
