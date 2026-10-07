# Windows installer for Frontier Usage. Double-click install-windows.bat (which runs this).
#
# 1. Finds a Python 3.8+ with Tk, or installs the latest Python for the current user only (no
#    admin rights needed): with winget if available, otherwise the official python.org installer.
#    python.org/winget builds include Tk and the "py" launcher.
# 2. Removes the "downloaded from the internet" mark from this folder so Windows doesn't block it.
# 3. Adds a "Frontier Usage" shortcut to the Desktop and Start menu.
#
# Test without changing anything:  powershell -ExecutionPolicy Bypass -File install-windows.ps1 -DryRun

param([switch]$DryRun)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is very slow with the progress bar
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$FallbackVersion = '3.12.10'   # used only if python.org's download page can't be read
$WingetId = 'Python.Python.3.13'

function Say($msg)  { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "    ! $msg" -ForegroundColor Yellow }
function Fail($msg) { Write-Host "`nInstall failed: $msg" -ForegroundColor Red; exit 1 }
function Run([scriptblock]$cmd) { if ($DryRun) { Write-Host "    + $cmd" } else { & $cmd } }

# Returns the full path of a python.exe that is 3.8+ and has Tk, or $null.
function Test-Python([string]$exe, [string[]]$pre = @()) {
    try {
        $out = & $exe @pre -c "import sys, _tkinter; assert sys.version_info >= (3, 8); print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) { return ($out | Select-Object -Last 1).Trim() }
    } catch { }
    return $null
}

function Find-Python {
    # Prefer the py launcher; "python" may be the Microsoft Store stub that only opens the Store.
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $p = Test-Python 'py' @('-3'); if ($p) { return $p }
    }
    foreach ($c in @(Get-Command python -All -ErrorAction SilentlyContinue)) {
        if ($c.Source -like '*\WindowsApps\*') { continue }
        $p = Test-Python $c.Source; if ($p) { return $p }
    }
    foreach ($exe in Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
                                   "$env:ProgramFiles\Python3*\python.exe" -ErrorAction SilentlyContinue |
                     Sort-Object FullName -Descending) {
        $p = Test-Python $exe.FullName; if ($p) { return $p }
    }
    return $null
}

function Update-SessionPath {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' +
                [Environment]::GetEnvironmentVariable('Path', 'Machine')
}

function Install-WithWinget {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { return $false }
    Say "Installing Python with winget ($WingetId)"
    Run { winget install -e --id $WingetId --scope user --silent --accept-package-agreements --accept-source-agreements | Out-Host }
    return ($DryRun -or $LASTEXITCODE -eq 0)
}

function Get-LatestPythonOrgVersion {
    try {
        $html = (Invoke-WebRequest 'https://www.python.org/downloads/windows/' -UseBasicParsing -TimeoutSec 20).Content
        $vers = [regex]::Matches($html, 'python-(\d+\.\d+\.\d+)-amd64\.exe') | ForEach-Object { [version]$_.Groups[1].Value }
        if ($vers) { return ($vers | Sort-Object -Descending | Select-Object -First 1).ToString() }
    } catch { }
    return $null
}

function Install-FromPythonOrg {
    $ver = Get-LatestPythonOrgVersion
    if (-not $ver) { Warn "couldn't read python.org; using $FallbackVersion"; $ver = $FallbackVersion }
    $arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'arm64' } else { 'amd64' }
    $url = "https://www.python.org/ftp/python/$ver/python-$ver-$arch.exe"
    $exe = Join-Path $env:TEMP "python-$ver-$arch.exe"
    Say "Downloading Python $ver ($arch) from python.org"
    Run { Invoke-WebRequest $url -OutFile $exe -UseBasicParsing }
    Say "Installing Python $ver for this user (takes a minute)"
    Run {
        $p = Start-Process $exe -Wait -PassThru -ArgumentList @(
            '/quiet', 'InstallAllUsers=0', 'PrependPath=1', 'Include_tcltk=1', 'Include_pip=1',
            'Include_launcher=1', 'InstallLauncherAllUsers=0', 'Include_test=0', 'Shortcuts=0')
        if ($p.ExitCode -ne 0) { throw "python installer exited with code $($p.ExitCode)" }
    }
    Run { Remove-Item $exe -ErrorAction SilentlyContinue }
}

Say 'Frontier Usage installer for Windows'
$py = Find-Python
if ($py) {
    Write-Host "    Found $(& $py -V 2>&1) with Tk at $py"
} else {
    $ok = $false
    try { $ok = Install-WithWinget } catch { Warn "winget failed: $_" }
    if (-not $DryRun) { Update-SessionPath; $py = Find-Python }
    if (-not $py) {
        if ($ok -and -not $DryRun) { Warn 'winget finished but Python with Tk was not found; trying python.org' }
        try { Install-FromPythonOrg } catch { Fail "couldn't download or install Python ($_). Check your internet connection or proxy." }
        if (-not $DryRun) { Update-SessionPath; $py = Find-Python }
    }
    if ($DryRun) { $py = "$env:LOCALAPPDATA\Programs\Python\Python3XX\python.exe" }
    if (-not $py) { Fail 'Python was installed but could not be found. Sign out and back in, then run this again.' }
    Write-Host "    Installed $py"
}

# Not strictly needed on Windows (Python uses the system certificate store), but keeps HTTPS
# working behind TLS-inspecting proxies that add their own roots to certifi-based setups.
Say 'Installing Python packages (certifi)'
Run { & $py -m pip install --user --upgrade --disable-pip-version-check --quiet certifi }
if (-not $DryRun -and $LASTEXITCODE -ne 0) { Warn 'pip could not install certifi (not required on Windows)' }

Say 'Setting up shortcuts'
Run { Get-ChildItem -LiteralPath $Here -Recurse -File | Unblock-File }
$pyw = Join-Path (Split-Path $py) 'pythonw.exe'
if (-not (Test-Path $pyw)) { $pyw = $py }
$shell = New-Object -ComObject WScript.Shell
$targets = @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))
foreach ($dir in $targets) {
    $lnk = Join-Path $dir 'Frontier Usage.lnk'
    if ($DryRun) { Write-Host "    + shortcut $lnk -> $pyw gui.py"; continue }
    $s = $shell.CreateShortcut($lnk)
    $s.TargetPath = $pyw
    $s.Arguments = '"' + (Join-Path $Here 'gui.py') + '"'
    $s.WorkingDirectory = $Here
    $s.Description = 'Token usage by user across AI providers'
    $s.IconLocation = "$py,0"
    $s.Save()
}

Say 'Done. Double-click "Frontier Usage" on your Desktop (or in the Start menu) to start.'
if (-not $DryRun) {
    $ans = Read-Host 'Open it now? [Y/n]'
    if ($ans -eq '' -or $ans -match '^[Yy]') { Start-Process $pyw -ArgumentList ('"' + (Join-Path $Here 'gui.py') + '"') -WorkingDirectory $Here }
}
