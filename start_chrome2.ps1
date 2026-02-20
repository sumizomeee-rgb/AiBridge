Stop-Process -Name chrome -Force -ErrorAction SilentlyContinue
Start-Sleep 3
# Double check
Stop-Process -Name chrome -Force -ErrorAction SilentlyContinue
Start-Sleep 3
# Verify all dead
$count = (Get-Process chrome -ErrorAction SilentlyContinue | Measure-Object).Count
Write-Output "Chrome processes after kill: $count"
if ($count -gt 0) {
    Write-Output "WARN: Chrome still running, trying taskkill"
    taskkill /F /IM chrome.exe 2>$null
    Start-Sleep 3
}
$count2 = (Get-Process chrome -ErrorAction SilentlyContinue | Measure-Object).Count
Write-Output "Chrome processes final: $count2"
Start-Process "C:\Program Files\Google\Chrome\Application\chrome.exe" -ArgumentList "--remote-debugging-port=9222","--disable-background-mode","--no-first-run"
Start-Sleep 6
$count3 = (Get-Process chrome -ErrorAction SilentlyContinue | Measure-Object).Count
Write-Output "Chrome processes after start: $count3"
try {
    $r = Invoke-WebRequest -Uri "http://localhost:9222/json/version" -UseBasicParsing -TimeoutSec 5
    Write-Output "CDP OK"
} catch {
    Write-Output "CDP FAIL"
    netstat -ano | Select-String "LISTENING" | Select-String "chrome"
}
