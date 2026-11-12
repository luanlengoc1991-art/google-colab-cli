# Build "Colab Studio.exe" (một tệp, không cần cài Python). Chạy:  powershell -File studio\build.ps1
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
$py = '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { uv sync; uv pip install --python $py pywebview==6.2.1 pyinstaller==6.22.0 pillow }
& $py studio\make_icon.py | Out-Null
& $py -m PyInstaller --noconfirm --onefile --windowed --log-level WARN --name 'Colab Studio' `
  --icon "$PWD\studio\icon.ico" `
  --add-data "$PWD\studio\ui:ui" `
  --collect-all colab_cli --collect-all webview --copy-metadata google-colab-cli `
  --hidden-import keyring.backends.Windows `
  --distpath studio\dist --workpath studio\build --specpath studio\build studio\colab_studio.py
if (-not (Test-Path 'studio\dist\Colab Studio.exe')) { throw 'Build that bai' }
Get-Item 'studio\dist\Colab Studio.exe' | Select-Object Name, @{n='MB';e={[math]::Round($_.Length/1MB,1)}}
