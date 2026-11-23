<#
.SYNOPSIS
    ChronoX Finance Desktop — One-Command Windows Setup & Installer Builder (x64 & ARM64).

.DESCRIPTION
    Checks Python 3.11+ and Node.js 20+, creates `.venv`, installs ChronoX Finance and
    Electron desktop dependencies, generates Windows `.ico` and PNG brand assets, and
    optionally bundles the PyInstaller zero-Python sidecar + NSIS/Portable installers
    or launches the desktop app directly.

.PARAMETER BuildInstaller
    Bundle the standalone backend sidecar and build Windows NSIS Setup + Portable `.exe`
    installers in `desktop\dist\`.

.PARAMETER SkipBackend
    Skip bundling the PyInstaller backend sidecar when using -BuildInstaller.

.PARAMETER Launch
    Launch ChronoX Finance Desktop immediately after setup.

.PARAMETER Arch
    Target Windows architecture: auto (default), x64, or arm64.

.EXAMPLE
    .\scripts\install-windows.ps1 -Launch

.EXAMPLE
    .\scripts\install-windows.ps1 -BuildInstaller

.EXAMPLE
    .\scripts\install-windows.ps1 -BuildInstaller -Arch arm64
#>
[CmdletBinding()]
param(
    [switch]$BuildInstaller,
    [switch]$SkipBackend,
    [switch]$Launch,
    [ValidateSet('auto', 'x64', 'arm64')]
    [string]$Arch = 'auto',
    [switch]$Help
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($Help) {
    Get-Help -Full $PSCommandPath
    exit 0
}

function Write-Info([string]$Message) {
    Write-Host "[chronox] $Message" -ForegroundColor Green
}

function Write-Warn([string]$Message) {
    Write-Warning "[chronox] $Message"
}

function Resolve-TargetArch([string]$Requested) {
    if ($Requested -ne 'auto') {
        return $Requested
    }
    try {
        $osArch = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString().ToLowerInvariant()
        if ($osArch -eq 'arm64') { return 'arm64' }
        if ($osArch -eq 'x64')   { return 'x64' }
    } catch {
        # Fallback for older Windows PowerShell 5.1 builds
    }
    $envArch = ($env:PROCESSOR_ARCHITEW6432, $env:PROCESSOR_ARCHITECTURE | Where-Object { $_ } | Select-Object -First 1)
    if ($envArch -and $envArch.ToUpperInvariant() -eq 'ARM64') {
        return 'arm64'
    }
    return 'x64'
}

function Resolve-SystemPython {
    $candidates = @(
        @{ Cmd = 'py';     Args = @('-3.12') },
        @{ Cmd = 'py';     Args = @('-3.13') },
        @{ Cmd = 'py';     Args = @('-3.11') },
        @{ Cmd = 'py';     Args = @('-3') },
        @{ Cmd = 'python'; Args = @() }
    )
    foreach ($cand in $candidates) {
        $found = Get-Command $cand.Cmd -ErrorAction SilentlyContinue
        if (-not $found) { continue }
        try {
            $checkArgs = @($cand.Args) + @('-c', 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)')
            & $cand.Cmd @checkArgs 2>$null
            if ($LASTEXITCODE -eq 0) {
                return $cand
            }
        } catch {
            continue
        }
    }
    return $null
}

$RepoRoot   = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$DesktopDir = Join-Path $RepoRoot 'desktop'
$VenvDir    = Join-Path $RepoRoot '.venv'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
$TargetArch = Resolve-TargetArch $Arch

Write-Info "ChronoX Finance Desktop Setup (Windows $TargetArch)"

# --- 1. Check Python 3.11+ ---
$PySpec = Resolve-SystemPython
if (-not $PySpec) {
    Write-Host ""
    Write-Host "[chronox] ERROR: Python 3.11+ (3.12 recommended) is required." -ForegroundColor Red
    Write-Host "Install it quickly with winget:" -ForegroundColor Yellow
    Write-Host "  winget install --id Python.Python.3.12 -e"
    exit 1
}
$pyVersionArgs = @($PySpec.Args) + @('--version')
$pyVersion = (& $PySpec.Cmd @pyVersionArgs 2>&1 | Out-String).Trim()
Write-Info "Using Python: $pyVersion"

# --- 2. Check Node.js 20+ & npm ---
if (-not (Get-Command node -ErrorAction SilentlyContinue) -or -not (Get-Command npm -ErrorAction SilentlyContinue)) {
    Write-Host ""
    Write-Host "[chronox] ERROR: Node.js 20+ and npm are required." -ForegroundColor Red
    Write-Host "Install it quickly with winget:" -ForegroundColor Yellow
    Write-Host "  winget install --id OpenJS.NodeJS.LTS -e"
    exit 1
}

$nodeVersion = (& node -v).Trim()
$nodeMajor = [int](& node -p 'parseInt(process.versions.node.split(".")[0], 10)')
if ($nodeMajor -lt 20) {
    Write-Host "[chronox] ERROR: Node.js 20+ is required (found $nodeVersion)." -ForegroundColor Red
    Write-Host "  winget install --id OpenJS.NodeJS.LTS -e"
    exit 1
}
$npmVersion = (& npm -v).Trim()
Write-Info "Using Node.js $nodeVersion / npm $npmVersion"

# --- 3. Create .venv & Install ChronoX Finance ---
$hasUv = [bool](Get-Command uv -ErrorAction SilentlyContinue)
if (-not (Test-Path $VenvPython)) {
    Write-Info "Creating Python virtual environment at $VenvDir..."
    if ($hasUv) {
        & uv venv $VenvDir --python 3.12
        if ($LASTEXITCODE -ne 0) {
            $venvArgs = @($PySpec.Args) + @('-m', 'venv', $VenvDir)
            & $PySpec.Cmd @venvArgs
        }
    } else {
        $venvArgs = @($PySpec.Args) + @('-m', 'venv', $VenvDir)
        & $PySpec.Cmd @venvArgs
    }
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $VenvPython)) {
        throw "Failed to create Python virtual environment at $VenvDir"
    }
}

Write-Info "Installing ChronoX Finance Python dependencies into .venv..."
if ($hasUv) {
    & uv pip install --python $VenvPython -e $RepoRoot
} else {
    & $VenvPython -m pip install --upgrade pip | Out-Null
    & $VenvPython -m pip install -e $RepoRoot
}
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install ChronoX Finance Python package."
}

# --- 4. Install Electron Desktop Dependencies & Icons ---
Push-Location $DesktopDir
try {
    Write-Info "Installing Electron desktop dependencies in desktop\..."
    if (Test-Path (Join-Path $DesktopDir 'package-lock.json')) {
        & npm ci --no-audit --no-fund
    } else {
        & npm install --no-audit --no-fund
    }
    if ($LASTEXITCODE -ne 0) { throw "npm install failed." }

    Write-Info "Generating Windows .ico and PNG brand icons..."
    & npm run icons
    if ($LASTEXITCODE -ne 0) { throw "Icon generation failed." }

    # --- 5. Optional Windows Installer Build (NSIS + Portable) ---
    if ($BuildInstaller) {
        if (-not $SkipBackend) {
            Write-Info "Ensuring PyInstaller is installed in .venv..."
            if ($hasUv) {
                & uv pip install --python $VenvPython pyinstaller
            } else {
                & $VenvPython -m pip install pyinstaller
            }
            if ($LASTEXITCODE -ne 0) { throw "Failed to install PyInstaller." }

            Write-Info "Bundling zero-dependency Windows backend sidecar (chronox-backend.exe)..."
            $env:CHRONOX_PYTHON = $VenvPython
            & npm run build:backend
            if ($LASTEXITCODE -ne 0) { throw "Backend sidecar build failed." }
        } else {
            Write-Warn "Skipping PyInstaller backend bundle (-SkipBackend)."
        }

        Write-Info "Building Windows NSIS installer & Portable .exe for $TargetArch..."
        & npx electron-builder --config electron-builder.yml --win nsis portable "--$TargetArch" --publish never
        if ($LASTEXITCODE -ne 0) { throw "electron-builder Windows packaging failed." }

        Write-Info "Build complete! Installers in $DesktopDir\dist\:"
        Get-ChildItem -Path (Join-Path $DesktopDir 'dist') -Filter '*.exe' -ErrorAction SilentlyContinue |
            ForEach-Object { Write-Host ("  - {0} ({1:N1} MB)" -f $_.Name, ($_.Length / 1MB)) }
    }

    # --- 6. Optional Immediate Launch ---
    if ($Launch) {
        Write-Info "Launching ChronoX Finance Desktop..."
        & npm start
    } elseif (-not $BuildInstaller) {
        Write-Info "Setup complete! Start ChronoX Finance anytime with:"
        Write-Host "  cd desktop; npm start                            # Launch encrypted local vault"
        Write-Host "  .\scripts\install-windows.ps1 -BuildInstaller   # Build .exe installers"
    }
} finally {
    Pop-Location
}
