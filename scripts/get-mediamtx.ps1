# Downloads the official MediaMTX Windows release from GitHub (bluenviron/mediamtx) into tools\mediamtx\
# and verifies its SHA-256 against the release's checksums file.
param([string]$Version = "v1.21.4")
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$dest = Join-Path $root "tools\mediamtx"
New-Item -ItemType Directory -Force $dest | Out-Null
$asset = "mediamtx_${Version}_windows_amd64.zip"
$base = "https://github.com/bluenviron/mediamtx/releases/download/$Version"
$zip = Join-Path $env:TEMP $asset
Write-Host "Downloading $asset ..."
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Invoke-WebRequest "$base/$asset" -OutFile $zip -UseBasicParsing
$sumFile = Join-Path $env:TEMP "mediamtx_checksums.sha256"
Invoke-WebRequest "$base/checksums.sha256" -OutFile $sumFile -UseBasicParsing
$line = Get-Content $sumFile | Where-Object { $_ -match [regex]::Escape($asset) } | Select-Object -First 1
Remove-Item $sumFile
$expected = ($line -split "\s+")[0]
$actual = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
if (-not $expected -or $expected.Trim().ToLower() -ne $actual) { throw "Checksum mismatch for $asset" }
Expand-Archive $zip -DestinationPath $dest -Force
Remove-Item $zip
Write-Host "MediaMTX $Version installed in $dest (checksum verified)."
