$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = if ($env:PYTHON) { $env:PYTHON } else { (Get-Command python).Source }
$OutputDir = if ($env:OUTPUT_DIR) { $env:OUTPUT_DIR } else { Join-Path $ProjectDir "dist" }
$AdbDir = if ($env:ADB_DIR) { $env:ADB_DIR } else { Join-Path $ProjectDir "assets\adb" }
$DisplayName = "Android" + (-join (0x5E94, 0x7528, 0x6E05, 0x9664, 0x52A9, 0x624B | ForEach-Object { [char]$_ }))

if (-not (Test-Path -LiteralPath (Join-Path $AdbDir "adb.exe"))) {
    $DependencyDir = [IO.Path]::GetFullPath((Join-Path $ProjectDir "build\dependencies"))
    $Archive = Join-Path $DependencyDir "platform-tools.zip"
    $Extracted = [IO.Path]::GetFullPath((Join-Path $DependencyDir "platform-tools"))
    New-Item -ItemType Directory -Force -Path $DependencyDir | Out-Null
    Invoke-WebRequest `
        -Uri "https://dl.google.com/android/repository/platform-tools-latest-windows.zip" `
        -OutFile $Archive
    if (Test-Path -LiteralPath $Extracted) {
        if (-not $Extracted.StartsWith([IO.Path]::GetFullPath($ProjectDir), [StringComparison]::OrdinalIgnoreCase)) {
            throw "拒绝删除项目目录以外的依赖目录：$Extracted"
        }
        Remove-Item -LiteralPath $Extracted -Recurse -Force
    }
    Expand-Archive -LiteralPath $Archive -DestinationPath $DependencyDir -Force
    $AdbDir = $Extracted
}

& $Python -m pip install -r (Join-Path $ProjectDir "requirements-dev.txt")

& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name "AndroidAppRemovalAssistant" `
    --icon "$ProjectDir\assets\icon\app-icon.ico" `
    --version-file "$ProjectDir\version_info.txt" `
    --distpath $OutputDir `
    --workpath (Join-Path $ProjectDir "build") `
    --specpath $ProjectDir `
    --collect-all apkutils2 `
    --add-binary "$AdbDir\adb.exe;adb" `
    --add-binary "$AdbDir\AdbWinApi.dll;adb" `
    --add-binary "$AdbDir\AdbWinUsbApi.dll;adb" `
    --add-binary "$ProjectDir\assets\aapt2\aapt2.exe;aapt2" `
    --add-data "$ProjectDir\assets\aapt2\NOTICE;aapt2" `
    --add-data "$ProjectDir\assets\icon\app-icon.ico;icon" `
    (Join-Path $ProjectDir "app.py")

$BuiltExe = Join-Path $OutputDir "AndroidAppRemovalAssistant.exe"
$FinalExe = Join-Path $OutputDir "$DisplayName.exe"
if (Test-Path -LiteralPath $FinalExe) {
    Remove-Item -LiteralPath $FinalExe -Force
}
Move-Item -LiteralPath $BuiltExe -Destination $FinalExe
