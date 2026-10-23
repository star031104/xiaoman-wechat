# xiaoman-wechat one-line installer for Windows (PowerShell):
#   irm https://raw.githubusercontent.com/star031104/xiaoman-wechat/main/get.ps1 | iex
# Downloads the latest code to Documents\xiaoman-wechat (keeps home\, .venv if they exist)
# and runs install.bat. GitHub too slow? It retries through public mirrors.
$ErrorActionPreference = "Stop"
$repo = "star031104/xiaoman-wechat"
$dest = if ($env:XIAOMAN_DIR) { $env:XIAOMAN_DIR } else { Join-Path ([Environment]::GetFolderPath("MyDocuments")) "xiaoman-wechat" }
$zipUrl = "https://github.com/$repo/archive/refs/heads/main.zip"
$mirrors = @("", "https://ghfast.top/", "https://gh-proxy.com/", "https://ghproxy.net/")
$tmp = Join-Path $env:TEMP ("xiaoman-wechat-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tmp | Out-Null
$zip = Join-Path $tmp "xiaoman-wechat.zip"
$ok = $false
foreach ($m in $mirrors) {
  try {
    Write-Host "Downloading $m$zipUrl"
    Invoke-WebRequest -Uri ($m + $zipUrl) -OutFile $zip -UseBasicParsing -TimeoutSec 120
    if ((Get-Item $zip).Length -gt 100000) { $ok = $true; break }
  } catch { Write-Host "  failed: $($_.Exception.Message)" }
}
if (-not $ok) { Write-Host "Download failed. Open https://github.com/$repo , click Code -> Download ZIP, unzip it and double-click install.bat."; exit 1 }
Expand-Archive -Path $zip -DestinationPath $tmp -Force
$src = Get-ChildItem $tmp -Directory | Where-Object { $_.Name -like "xiaoman-wechat*" } | Select-Object -First 1
New-Item -ItemType Directory -Force -Path $dest | Out-Null
# copy code over; never touch config.yaml / .env / data / napcat / .venv
robocopy $src.FullName $dest /E /NFL /NDL /NJH /NJS /XD data napcat .venv home /XF config.yaml config.llm.yaml .env | Out-Null
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "Code is in $dest"
Start-Process -FilePath (Join-Path $dest "install.bat") -WorkingDirectory $dest
