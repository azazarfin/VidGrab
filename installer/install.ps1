# ==============================================================================
# VidGrab Backend Installer (Windows)
# ==============================================================================

Write-Host "====================================================" -ForegroundColor Cyan
Write-Host "          VidGrab Backend Service Installer          " -ForegroundColor Cyan
Write-Host "====================================================" -ForegroundColor Cyan

# 1. Resolve paths
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$VenvDir = Join-Path $ProjectRoot "venv"

Write-Host "Project Root: $ProjectRoot" -ForegroundColor DarkGray

# 2. Check dependencies
Write-Host "`n[1/4] Auditing system dependencies..." -ForegroundColor Blue

# Check Python
$PythonCheck = Get-Command python -ErrorAction SilentlyContinue
if (-not $PythonCheck) {
    Write-Host "Error: Python is not installed or not in PATH." -ForegroundColor Red
    Write-Host "Please install Python 3.11+ and try again." -ForegroundColor Red
    Exit 1
}

$PythonVersion = & python -c "import sys; print('.'.join(map(str, sys.version_info[:2])))"
Write-Host "Found Python: v$PythonVersion" -ForegroundColor Green
& python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if ($LASTEXITCODE -ne 0) {
    Write-Host "VidGrab requires Python 3.11 or newer." -ForegroundColor Red
    Exit 1
}

# Check FFmpeg (Warning only)
$FfmpegCheck = Get-Command ffmpeg -ErrorAction SilentlyContinue
if (-not $FfmpegCheck) {
    Write-Host "Warning: FFmpeg was not found on your system." -ForegroundColor Yellow
    Write-Host "         VidGrab will still work, but advanced features like" -ForegroundColor Yellow
    Write-Host "         audio extraction, video recoding, and subtitle embedding" -ForegroundColor Yellow
    Write-Host "         will be disabled." -ForegroundColor Yellow
    Write-Host "         Recommendation: Install FFmpeg and add it to your PATH." -ForegroundColor Yellow
} else {
    Write-Host "Found FFmpeg: $($FfmpegCheck.Source)" -ForegroundColor Green
}

# 3. Create virtual environment
Write-Host "`n[2/4] Setting up Python virtual environment..." -ForegroundColor Blue
if (Test-Path $VenvDir) {
    Write-Host "Virtual environment already exists at $VenvDir" -ForegroundColor DarkGray
} else {
    & python -m venv $VenvDir
    Write-Host "Created virtual environment at $VenvDir" -ForegroundColor Green
}

# 4. Install dependencies
Write-Host "`n[3/4] Installing Python libraries..." -ForegroundColor Blue
$PipPath = Join-Path $VenvDir "Scripts\pip.exe"
$PythonExePath = Join-Path $VenvDir "Scripts\python.exe"

& $PipPath install --upgrade pip
& $PipPath install --require-hashes -r (Join-Path $ProjectRoot "backend\requirements.txt")
Write-Host "Dependencies installed successfully." -ForegroundColor Green

# 5. Configure Task Scheduler for background execution
Write-Host "`n[4/4] Configuring Scheduled Task for background execution..." -ForegroundColor Blue

$TaskName = "VidGrabBackend"
$UvicornPath = Join-Path $VenvDir "Scripts\uvicorn.exe"

if (Test-Path $UvicornPath) {
    # Check if task already exists, delete it first
    $ExistingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($ExistingTask) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed existing Scheduled Task: $TaskName" -ForegroundColor DarkGray
    }

    # Create new task to run on logon silently
    # Arguments: backend.main:app --host 127.0.0.1 --port 8000
    $Action = New-ScheduledTaskAction -Execute "$PythonExePath" -Argument "-m uvicorn backend.main:app --host 127.0.0.1 --port 8000" -WorkingDirectory "$ProjectRoot"
    $Trigger = New-ScheduledTaskTrigger -AtLogOn
    $Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive

    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Force | Out-Null
    
    # Start task now
    Start-ScheduledTask -TaskName $TaskName

    Write-Host "Scheduled Task '$TaskName' registered and started successfully!" -ForegroundColor Green
    Write-Host "Backend running in background at: http://127.0.0.1:8000" -ForegroundColor Green
} else {
    Write-Host "Error: Virtual environment python executable not found." -ForegroundColor Red
}

Write-Host "`n====================================================" -ForegroundColor Green
Write-Host "            VidGrab Installation Complete            " -ForegroundColor Green
Write-Host "====================================================" -ForegroundColor Green
Write-Host "To start/stop/restart backend manually, run uvicorn:"
Write-Host "  cd '$ProjectRoot'"
Write-Host "  .\venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000"
Write-Host "===================================================="
