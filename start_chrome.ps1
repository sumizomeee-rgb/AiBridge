Stop-Process -Name chrome -Force -ErrorAction SilentlyContinue
Start-Sleep 5
Start-Process "C:\Program Files\Google\Chrome\Application\chrome.exe" -ArgumentList "--remote-debugging-port=9222","--disable-background-mode"
Start-Sleep 5
try {
    $r = Invoke-WebRequest -Uri "http://localhost:9222/json/version" -UseBasicParsing -TimeoutSec 5
    Write-Output "CDP OK"
    Write-Output $r.Content
} catch {
    Write-Output "CDP FAIL"
}
