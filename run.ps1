Set-Location "$PSScriptRoot"

# Activate virtualenv if present
if (Test-Path ".venv/Scripts/Activate.ps1") {
    . ".venv/Scripts/Activate.ps1"
}

$env:FLASK_APP = "app.py"
$env:FLASK_ENV = "development"
$env:FLASK_DEBUG = "1"

Write-Host "Starting Flask on http://127.0.0.1:5000"
flask run
