# Build "Location Sound File Manager" for Windows (self-contained: Python, Qt
# and numpy inside) with PyInstaller. Needs Python 3.11+ (python.org or `winget
# install Python.Python.3.13`).
#   windows\build_app.ps1            build into dist\ and make a zip
#   windows\build_app.ps1 -Install   also copy it to %LOCALAPPDATA%\Programs and
#                                    add a Start menu shortcut
param([switch]$Install)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $PSScriptRoot
Set-Location $here
$name = "Location Sound File Manager"
$version = "1.6.0"
try {
    $tag = git describe --tags --abbrev=0 2>$null
    if ($tag) { $version = $tag -replace '^v', '' -replace '-(linux|macos|windows)$', '' }
} catch {}
$build = Join-Path $here "build\windows"

# A private venv, so the system Python stays untouched.
$python = Join-Path $here ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    if (Get-Command py -ErrorAction SilentlyContinue) { py -3 -m venv .venv } else { python -m venv .venv }
}
& $python -m pip install --quiet --upgrade pip
& $python -m pip install --quiet PySide6 numpy pyinstaller pillow
& $python scripts/build_native.py
if ($LASTEXITCODE -ne 0) { throw "Rust waveform build failed (install the Rust toolchain first)" }

# The app icon: an .ico from the 256 px PNG (sizes Windows uses in Explorer and the taskbar).
New-Item -ItemType Directory -Force $build | Out-Null
$ico = Join-Path $build "icon.ico"
& $python -c "from PIL import Image; Image.open(r'sound_file_manager/assets/icon.png').save(r'$ico', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])"

# Version info shown in the file's Properties.
$v = ($version.Split('.') + @('0', '0', '0', '0'))[0..3] -join ', '
$versionInfo = @"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($v), prodvers=($v)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('FileDescription', '$name'),
    StringStruct('ProductName', '$name'),
    StringStruct('FileVersion', '$version'),
    StringStruct('ProductVersion', '$version'),
    StringStruct('LegalCopyright', 'MIT License')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])]
)
"@
# Without a BOM: PyInstaller evaluates this file as Python.
[IO.File]::WriteAllText((Join-Path $build "version.txt"), $versionInfo, (New-Object Text.UTF8Encoding $false))

& $python -m PyInstaller --noconfirm --clean --windowed `
    --name $name `
    --icon $ico `
    --version-file (Join-Path $build "version.txt") `
    --add-data "$here\sound_file_manager\assets;sound_file_manager\assets" --paths $here `
    --add-binary "$here\sound_file_manager\_native\sfm_waveform.dll;sound_file_manager\_native" `
    --workpath (Join-Path $build "work") --specpath $build --distpath (Join-Path $here "dist") `
    (Join-Path $here "windows\launcher.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$app = Join-Path $here "dist\$name"
$zip = Join-Path $here "dist\Location-Sound-File-Manager-$version-windows-x64.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path $app -DestinationPath $zip
Write-Host "Built: $app\$name.exe"
Write-Host "Zip:   $zip"

if ($Install) {
    $target = Join-Path $env:LOCALAPPDATA "Programs\$name"
    if (Test-Path $target) { Remove-Item -Recurse -Force $target }
    Copy-Item -Recurse $app $target
    $shortcut = Join-Path ([Environment]::GetFolderPath("Programs")) "$name.lnk"
    $link = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcut)
    $link.TargetPath = Join-Path $target "$name.exe"
    $link.WorkingDirectory = $target
    $link.Save()
    Write-Host "Installed: $target (Start menu: $name)"
}
