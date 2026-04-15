<#
.SYNOPSIS
    PDF POC FastAPI 서버 실행/중단 스크립트

.EXAMPLE
    .\run.ps1 start         # 서버 시작 (백그라운드)
    .\run.ps1 stop          # 서버 중단
    .\run.ps1 status        # 상태 확인
    .\run.ps1 restart       # 재시작
    .\run.ps1 logs          # 실시간 로그
    .\run.ps1 start -Port 9000
    .\run.ps1 start -Foreground   # 현재 콘솔에서 실행 (Ctrl+C 로 중단)
#>
param(
    [Parameter(Position = 0)]
    [ValidateSet('start', 'stop', 'status', 'restart', 'logs')]
    [string]$Action = 'start',

    [int]$Port = 8000,
    [string]$BindHost = '127.0.0.1',
    [switch]$Foreground
)

$ErrorActionPreference = 'Stop'

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PidFile    = Join-Path $ScriptRoot '.pdf-poc.pid'
$LogFile    = Join-Path $ScriptRoot '.pdf-poc.log'
$ErrFile    = Join-Path $ScriptRoot '.pdf-poc.err.log'
$VenvPython = Join-Path $ScriptRoot '.venv\Scripts\python.exe'
$BackendDir = Join-Path $ScriptRoot 'backend'

function Get-RunningProc {
    if (-not (Test-Path $PidFile)) { return $null }
    $id = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
    if (-not $id) { return $null }
    $proc = Get-Process -Id $id -ErrorAction SilentlyContinue
    if ($proc -and -not $proc.HasExited) { return $proc }
    Remove-Item $PidFile -ErrorAction SilentlyContinue
    return $null
}

function Test-Prereq {
    if (-not (Test-Path $VenvPython)) {
        throw "가상환경 파이썬을 찾을 수 없습니다: $VenvPython`n먼저 'python -m venv .venv' 후 '.venv\Scripts\pip install -r backend\requirements.txt' 를 실행하세요."
    }
    if (-not (Test-Path $BackendDir)) {
        throw "backend 디렉터리가 없습니다: $BackendDir"
    }
}

function Start-ServerFg {
    Test-Prereq
    Write-Host "[fg] 서버 시작: http://${BindHost}:$Port  (Ctrl+C 로 중단)" -ForegroundColor Cyan
    Push-Location $BackendDir
    try {
        & $VenvPython -m uvicorn app:app --host $BindHost --port $Port
    }
    finally {
        Pop-Location
    }
}

function Start-ServerBg {
    Test-Prereq
    $running = Get-RunningProc
    if ($running) {
        Write-Host "이미 실행 중입니다 (PID=$($running.Id))" -ForegroundColor Yellow
        return
    }

    $argList = @('-m', 'uvicorn', 'app:app', '--host', $BindHost, '--port', "$Port")
    $proc = Start-Process -FilePath $VenvPython `
        -ArgumentList $argList `
        -WorkingDirectory $BackendDir `
        -RedirectStandardOutput $LogFile `
        -RedirectStandardError $ErrFile `
        -WindowStyle Hidden `
        -PassThru

    $proc.Id | Out-File -FilePath $PidFile -Encoding ascii -Force

    Start-Sleep -Milliseconds 800
    if ($proc.HasExited) {
        Write-Host "시작 실패. 로그 확인:" -ForegroundColor Red
        if (Test-Path $ErrFile) { Get-Content $ErrFile -Tail 30 }
        Remove-Item $PidFile -ErrorAction SilentlyContinue
        return
    }

    Write-Host "시작됨 (PID=$($proc.Id))" -ForegroundColor Green
    Write-Host "  URL : http://${BindHost}:$Port"
    Write-Host "  로그: $LogFile"
    Write-Host "  중단: .\run.ps1 stop"
}

function Stop-Server {
    $running = Get-RunningProc
    if (-not $running) {
        Write-Host "실행 중인 서버가 없습니다." -ForegroundColor Yellow
        return
    }
    $id = $running.Id
    # 프로세스 트리 전체 종료 (자식 워커 포함)
    & taskkill.exe /PID $id /T /F | Out-Null
    Remove-Item $PidFile -ErrorAction SilentlyContinue
    Write-Host "중단됨 (PID=$id)" -ForegroundColor Green
}

function Show-Status {
    $running = Get-RunningProc
    if ($running) {
        Write-Host "실행 중 — PID=$($running.Id), 시작시각=$($running.StartTime)" -ForegroundColor Green
        Write-Host "  URL : http://${BindHost}:$Port"
        Write-Host "  로그: $LogFile"
    }
    else {
        Write-Host "중지됨" -ForegroundColor Yellow
    }
}

function Show-Logs {
    if (-not (Test-Path $LogFile)) {
        Write-Host "로그 파일이 아직 없습니다: $LogFile" -ForegroundColor Yellow
        return
    }
    Write-Host "=== $LogFile (Ctrl+C 로 종료) ===" -ForegroundColor Cyan
    Get-Content $LogFile -Wait -Tail 50
}

switch ($Action) {
    'start' {
        if ($Foreground) { Start-ServerFg } else { Start-ServerBg }
    }
    'stop'    { Stop-Server }
    'status'  { Show-Status }
    'restart' {
        Stop-Server
        Start-Sleep -Milliseconds 500
        Start-ServerBg
    }
    'logs'    { Show-Logs }
}
