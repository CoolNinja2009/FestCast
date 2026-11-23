# Restart FestCast through the start.bat supervisor (run via restart.bat).
# Stops only the FestCast started by THIS folder's start.bat; start.bat sees it exit and starts it again.
# Refuses (changes nothing) if start.bat isn't running, so FestCast is never left stopped.
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$startBat = Join-Path $root 'start.bat'

function Get-Supervisors {
    @(Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine.ToLower().Contains($startBat.ToLower()) })
}
function Get-FestCast($cmdPid) {
    # the python started directly by start.bat (the venv launcher); the real server is its child
    @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$cmdPid AND Name='python.exe'" |
        Where-Object { $_.CommandLine -match 'backend\.main' })
}
function Fail($msg) { Write-Host ''; Write-Host "  [restart] $msg" -ForegroundColor Red; exit 1 }

$sup = Get-Supervisors
if ($sup.Count -eq 0) { Fail 'start.bat is not running, so nothing was restarted. Start FestCast with start.bat.' }
if ($sup.Count -gt 1) { Fail "start.bat is running $($sup.Count) times. Close the extra windows first; nothing was restarted." }
$cmdPid = $sup[0].ProcessId

# start.bat may be in its 3-second pause between restarts: give it a moment
$old = @()
for ($i = 0; $i -lt 20 -and $old.Count -eq 0; $i++) { $old = Get-FestCast $cmdPid; if ($old.Count -eq 0) { Start-Sleep -Milliseconds 500 } }
if ($old.Count -eq 0) { Fail 'start.bat is open but FestCast is not running in it. Look at the FestCast window.' }

$port = 8000
try { $port = [int]((Get-Content (Join-Path $root 'data\ports.json') -Raw | ConvertFrom-Json).port) } catch {}

Write-Host "  [restart] Restarting FestCast (OBS keeps recording and streaming; cameras blink for a second or two)..."
try { Add-Content (Join-Path $root 'data\logs\restarts.log') "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') manual restart via restart.bat (the next 'exited with code 1' line is this)" } catch {}
foreach ($p in $old) { & taskkill.exe /PID $p.ProcessId /T /F | Out-Null }

# wait for start.bat to start a new FestCast
$oldIds = $old.ProcessId
$new = $null
for ($i = 0; $i -lt 60 -and -not $new; $i++) {
    Start-Sleep -Milliseconds 500
    if ((Get-Supervisors).Count -eq 0) { Fail 'The start.bat window was closed. Start FestCast with start.bat.' }
    $new = Get-FestCast $cmdPid | Where-Object { $oldIds -notcontains $_.ProcessId } | Select-Object -First 1
}
if (-not $new) { Fail 'start.bat did not start FestCast again within 30 s. Look at the FestCast window.' }

# wait until it answers HTTP (any answer, even 401, means the server is up)
$url = "http://127.0.0.1:$port/api/health"
for ($i = 0; $i -lt 60; $i++) {
    try { Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 2 | Out-Null; $up = $true } catch { $up = [bool]$_.Exception.Response }
    if ($up) { Write-Host "  [restart] FestCast is back (pid $($new.ProcessId), port $port)." -ForegroundColor Green; exit 0 }
    Start-Sleep -Seconds 1
}
Fail "FestCast started again but does not answer on port $port yet. Look at the FestCast window."
