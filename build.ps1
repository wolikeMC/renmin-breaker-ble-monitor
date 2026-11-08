# 在项目根目录执行：powershell -ExecutionPolicy Bypass -File .\build.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

python -m pip install -r requirements.txt

python -m PyInstaller `
  --noconfirm `
  --clean `
  --onefile `
  --windowed `
  --name "YUNM-Plug-Monitor" `
  --icon "app.ico" `
  --add-data "app.ico;." `
  --hidden-import "pystray._win32" `
  --hidden-import "bleak.backends.winrt.client" `
  --hidden-import "bleak.backends.winrt.scanner" `
  main.py

Write-Host ""
Write-Host "Build complete: dist\YUNM-Plug-Monitor.exe" -ForegroundColor Green
