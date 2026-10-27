# Install OpenRazer Studio, break it, repair it and remove it, checking each step.
#
#   studio/tools/installer-cycle.ps1 -Setup path\to\openrazer-studio-setup.exe -Dir C:\some\scratch\folder
#
# It installs for the current user and leaves nothing behind when it passes:
# the install folder, its Start menu entries and its Apps & features entry all
# go again. Point LOCALAPPDATA at a scratch folder first to keep the run's
# settings files out of your own.
param([Parameter(Mandatory)] [string]$Setup, [Parameter(Mandatory)] [string]$Dir)
$ErrorActionPreference = 'Stop'
$key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\OpenRazerStudio'
$menu = Join-Path ([Environment]::GetFolderPath('Programs')) 'OpenRazer Studio'
$failed = $false
function Check($what, $ok) { if ($ok) { "ok   $what" } else { "FAIL $what"; $script:failed = $true } }
$version = (& $Setup --version | Out-String).Trim().Split(' ')[-1]

& $Setup --install --silent --dir $Dir --no-autostart | Out-Host
Check 'install exit code' ($LASTEXITCODE -eq 0)
foreach ($f in 'openrazer-studio.exe', 'openrazer-win.exe', 'setup.exe', 'install.json', 'LICENSE.txt') { Check "installed $f" (Test-Path (Join-Path $Dir $f)) }
$reg = Get-ItemProperty $key
Check 'registered in Apps & features' ($reg.DisplayVersion -eq $version -and $reg.InstallLocation -eq $Dir)
Check 'uninstall command' ($reg.UninstallString -like '*setup.exe" --uninstall')
Check 'modify command' ($reg.ModifyPath -like '*setup.exe" --maintenance')
Check 'start menu entry' (Test-Path (Join-Path $menu 'OpenRazer Studio.lnk'))
Check 'start menu repair entry' ((Get-ChildItem $menu -Filter '*.lnk').Count -eq 2)
$run = (Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -ErrorAction SilentlyContinue).OpenRazerWin
Check 'autostart left alone' (-not ($run -like "*$Dir*"))

# Break it: a missing daemon, a truncated app, unreadable settings, no Start menu.
Remove-Item (Join-Path $Dir 'openrazer-win.exe')
Set-Content -Path (Join-Path $Dir 'openrazer-studio.exe') -Value 'broken' -NoNewline
$settings = Join-Path $env:LOCALAPPDATA 'openrazer-win\studio\settings.json'
New-Item -ItemType Directory -Force (Split-Path $settings) | Out-Null
Get-ChildItem (Split-Path $settings) -Filter 'settings.json.broken-*' | Remove-Item
Set-Content -Path $settings -Value '{ this is not json' -NoNewline
Remove-Item -Recurse $menu

& $Setup --repair --silent | Out-Host
Check 'repair exit code' ($LASTEXITCODE -eq 0)
Check 'daemon put back' (Test-Path (Join-Path $Dir 'openrazer-win.exe'))
Check 'app replaced' ((Get-Item (Join-Path $Dir 'openrazer-studio.exe')).Length -gt 1MB)
Check 'start menu put back' (Test-Path (Join-Path $menu 'OpenRazer Studio.lnk'))
Check 'broken settings set aside' (-not (Test-Path $settings) -and (Get-ChildItem (Split-Path $settings) -Filter 'settings.json.broken-*').Count -eq 1)
& (Join-Path $Dir 'openrazer-studio.exe') --version | Out-Host
Check 'repaired app runs' ($LASTEXITCODE -eq 0)

# Remove it with the copy Apps & features runs, which hands over to a copy in TEMP.
& (Join-Path $Dir 'setup.exe') --uninstall --silent | Out-Host
for ($i = 0; $i -lt 60 -and (Test-Path $Dir); $i++) { Start-Sleep -Milliseconds 500 }
Check 'install folder gone' (-not (Test-Path $Dir))
Check 'unregistered' (-not (Test-Path $key))
Check 'start menu gone' (-not (Test-Path $menu))
Check 'settings kept by default' ((Get-ChildItem (Split-Path $settings) -Filter 'settings.json.broken-*').Count -eq 1)
Start-Sleep -Seconds 6
Check 'temporary uninstaller cleaned up' ((Get-ChildItem $env:TEMP -Filter 'openrazer-studio-uninstall-*.exe' -ErrorAction SilentlyContinue).Count -eq 0)
if ($failed) { exit 1 }
'ALL OK'
exit 0
