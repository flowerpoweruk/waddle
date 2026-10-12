$ErrorActionPreference = 'Stop'
$appRoot = $PSScriptRoot
$appUrl = 'http://127.0.0.1:8765/'
try {
    $reply = Invoke-WebRequest -Uri $appUrl -UseBasicParsing -TimeoutSec 2
    if ($reply.Content -like '*<title>Waddle</title>*' -or $reply.Content -like '*<title>AI Overview Tool</title>*') {
        Start-Process $appUrl
        exit
    }
} catch { }
$localPython = Join-Path $appRoot '.venv\Scripts\pythonw.exe'
$pythonCommand = if (Test-Path -LiteralPath $localPython) { [pscustomobject]@{ Source = $localPython } } else { Get-Command pythonw.exe -ErrorAction SilentlyContinue }
if (-not $pythonCommand) {
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
}
if (-not $pythonCommand) {
    throw 'Python 3.10 or later is required. Install Python, then launch again.'
}
$appScript = Join-Path $appRoot 'app.py'
Start-Process -FilePath $pythonCommand.Source -ArgumentList @('"' + $appScript + '"') -WorkingDirectory $appRoot -WindowStyle Hidden
