param(
    [Parameter(Mandatory=$true)][string]$Url,
    [string]$Login,
    [string]$Name = 'book',
    [string]$Output = '',
    [switch]$Headful
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not $Output) { $Output = Join-Path $PSScriptRoot 'books' }
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$taskReady = Join-Path $PSScriptRoot '.venv\.litres-dependencies-ready'
if (-not (Test-Path -LiteralPath $taskPython) -or -not (Test-Path -LiteralPath $taskReady)) {
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        if (-not (Test-Path -LiteralPath $taskPython)) {
            uv venv .venv --python 3.12
            if ($LASTEXITCODE -ne 0) { throw 'Не удалось создать Python-окружение.' }
        }
        uv pip install --python $taskPython -r requirements-text.txt
    } else {
        if (-not (Test-Path -LiteralPath $taskPython)) {
            python -m venv .venv
            if ($LASTEXITCODE -ne 0) { throw 'Установите Python 3.12 или uv.' }
        }
        & $taskPython -m pip install -r requirements-text.txt
    }
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить зависимости.' }
    New-Item -ItemType File -Path $taskReady -Force | Out-Null
}
$taskArgs = @('-X','utf8','text_downloader.py','--url',$Url,'--output',$Output,'--name',$Name)
if ($Login) { $taskArgs += @('--login',$Login) }
if ($Headful) { $taskArgs += '--headful' }
& $taskPython @taskArgs
exit $LASTEXITCODE
