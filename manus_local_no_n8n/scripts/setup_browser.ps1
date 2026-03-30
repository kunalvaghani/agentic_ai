$ErrorActionPreference = 'Stop'
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (!(Test-Path $Python)) { throw 'Create the virtual environment first: py -3 -m venv .venv' }
& $Python -m pip install -r (Join-Path $ProjectRoot 'requirements.txt')
& $Python -m playwright install chromium
Write-Host 'Playwright Chromium installed.'
