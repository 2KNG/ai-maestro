# ==============================================================================
# ai-maestro - Windows installer (PowerShell 5.1+ / 7+)
# Repository: https://github.com/2KNG/ai-maestro
#
# Usage:
#   git clone https://github.com/2KNG/ai-maestro.git $HOME\dev\ai-maestro
#   powershell -ExecutionPolicy Bypass -File $HOME\dev\ai-maestro\install.ps1
#
# After installing, runs the 3-step onboarding (`maestro setup`):
#   [1/3] Antigravity / Codex / Claude CLIs  [2/3] Orca  [3/3] remote hub address
# Extra arguments are passed to `maestro setup`, e.g.:
#   .\install.ps1 --yes --hub <your-server-domain-or-ip>
# Set $env:MAESTRO_SETUP = "0" to skip onboarding.
# ==============================================================================
$ErrorActionPreference = "Stop"
$SetupArgs = $args

$RepoUrl = "https://github.com/2KNG/ai-maestro.git"
$DefaultDir = if ($env:MAESTRO_DIR) { $env:MAESTRO_DIR } else { Join-Path $HOME "dev\ai-maestro" }
$InstallDir = $DefaultDir
if ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot "skills\multi-agent-orchestrator"))) {
    $InstallDir = $PSScriptRoot
}

Write-Host "[ai-maestro] Installing / syncing multi-agent orchestrator"
Write-Host "   Repo path: $InstallDir"

# Python: prefer the py launcher, then python.
$Python = $null
$PythonArgs = @()
if (Get-Command py -ErrorAction SilentlyContinue) { $Python = "py"; $PythonArgs = @("-3") }
elseif (Get-Command python -ErrorAction SilentlyContinue) { $Python = "python" }
if (-not $Python) { throw "Python 3.10+ is required (https://www.python.org/downloads/windows/)" }

if (-not (Test-Path (Join-Path $InstallDir "skills\multi-agent-orchestrator"))) {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw "git is required to clone $RepoUrl" }
    if (Test-Path $InstallDir) { throw "$InstallDir exists but is not an ai-maestro checkout; refusing to overwrite" }
    New-Item -ItemType Directory -Force -Path (Split-Path $InstallDir) | Out-Null
    git clone $RepoUrl $InstallDir
    if ($LASTEXITCODE -ne 0) { throw "git clone failed" }
}

$ScriptsDir = Join-Path $InstallDir "skills\multi-agent-orchestrator\scripts"

# Preserve and validate existing Codex settings before any change (fails closed).
& $Python @PythonArgs (Join-Path $ScriptsDir "codex_config_safe.py")
if ($LASTEXITCODE -ne 0) { throw "Codex configuration check failed; nothing was changed" }

# 1. CLI wrappers in ~/.local/bin (added to the user PATH once).
$BinDir = Join-Path $HOME ".local\bin"
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
$Launcher = if ($Python -eq "py") { "py -3" } else { "python" }
$Wrappers = @{
    "maestro.cmd"         = "maestro.py"
    "agy-orca.cmd"        = "maestro.py"
    "ai-usage.cmd"        = "check_all_usage.py"
    "ai-usage-widget.cmd" = "render_usage_widget.py"
}
foreach ($name in $Wrappers.Keys) {
    $target = Join-Path $ScriptsDir $Wrappers[$name]
    Set-Content -Path (Join-Path $BinDir $name) -Encoding ASCII -Value "@echo off`r`n$Launcher `"$target`" %*"
}
Set-Content -Path (Join-Path $BinDir "ai-usage-web.cmd") -Encoding ASCII -Value "@echo off`r`nstart `"`" http://127.0.0.1:18765/"
$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not $UserPath) { $UserPath = "" }
if (-not (($UserPath -split ";") -contains $BinDir)) {
    [Environment]::SetEnvironmentVariable("Path", ($UserPath.TrimEnd(";") + ";" + $BinDir).TrimStart(";"), "User")
    Write-Host "   Added $BinDir to the user PATH (open a new terminal to use 'maestro')"
}
$env:Path = "$BinDir;$env:Path"
Write-Host "   Linked CLI commands in $BinDir (maestro, ai-usage, ai-usage-widget, ai-usage-web)"

# 2. Antigravity skills & plugins as directory junctions (existing real folders are kept).
function Link-Dir($Link, $Target) {
    if (Test-Path $Link) {
        $item = Get-Item $Link -Force
        if (-not ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            Write-Host "   Kept existing folder (not a link): $Link"
            return
        }
        $item.Delete()
    }
    New-Item -ItemType Directory -Force -Path (Split-Path $Link) | Out-Null
    New-Item -ItemType Junction -Path $Link -Target $Target | Out-Null
}
$Skill = Join-Path $InstallDir "skills\multi-agent-orchestrator"
$Plugin = Join-Path $InstallDir "plugins\codex-claude-bridge"
Link-Dir (Join-Path $HOME ".gemini\config\skills\multi-agent-orchestrator") $Skill
Link-Dir (Join-Path $HOME ".gemini\antigravity\skills\multi-agent-orchestrator") $Skill
Link-Dir (Join-Path $HOME ".gemini\config\plugins\codex-claude-bridge") $Plugin
foreach ($sub in @("codex", "claude")) {
    $src = Join-Path $Plugin "skills\$sub"
    if (Test-Path $src) {
        Link-Dir (Join-Path $HOME ".gemini\config\skills\$sub") $src
        Link-Dir (Join-Path $HOME ".gemini\antigravity\skills\$sub") $src
    }
}
Write-Host "   Linked Antigravity skills & codex-claude-bridge plugin"

# 3. Global agent rules (__HOME__ token -> current home).
$Rules = @{
    "GEMINI.md"           = Join-Path $HOME ".gemini\GEMINI.md"
    "AGENTS.workspace.md" = Join-Path $HOME "dev\AGENTS.md"
    "AGENTS.codex.md"     = Join-Path $HOME ".codex\AGENTS.md"
    "CLAUDE.md"           = Join-Path $HOME ".claude\CLAUDE.md"
}
foreach ($name in $Rules.Keys) {
    $dest = $Rules[$name]
    New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
    $text = [IO.File]::ReadAllText((Join-Path $InstallDir "rules\$name")).Replace("__HOME__", $HOME)
    [IO.File]::WriteAllText($dest, $text, (New-Object Text.UTF8Encoding $false))
}
Write-Host "   Installed global rules (GEMINI.md, dev\AGENTS.md, .codex\AGENTS.md, .claude\CLAUDE.md)"

# 4. Live widget bridge (port 18765) for this session; `maestro remote up` registers
#    a persistent Scheduled Task for remote nodes.
$Listening = $false
try { $Listening = [bool](Get-NetTCPConnection -LocalPort 18765 -State Listen -ErrorAction SilentlyContinue) } catch { }
if (-not $Listening) {
    $Pythonw = if ($Python -eq "py") { "pyw" } else { "pythonw" }
    if (-not (Get-Command $Pythonw -ErrorAction SilentlyContinue)) { $Pythonw = $Python }
    $DaemonArgs = @()
    if ($Pythonw -eq "pyw" -or $Pythonw -eq "py") { $DaemonArgs += "-3" }
    $DaemonArgs += "`"$(Join-Path $ScriptsDir 'widget_bridge_daemon.py')`""
    Start-Process -FilePath $Pythonw -ArgumentList $DaemonArgs -WindowStyle Hidden
    Write-Host "   Started widget bridge (http://127.0.0.1:18765)"
} else {
    Write-Host "   Widget bridge already listening on 18765 (kept)"
}

# 5. 3-step onboarding: core CLIs -> Orca -> remote hub address.
if ($env:MAESTRO_SETUP -ne "0") {
    & $Python @PythonArgs (Join-Path $ScriptsDir "maestro.py") setup @SetupArgs
    if ($LASTEXITCODE -ne 0) { Write-Host "   maestro setup did not finish; rerun: maestro setup" }
}

Write-Host ""
Write-Host "[ai-maestro] Installation complete"
Write-Host "   Live dashboard : http://127.0.0.1:18765"
Write-Host "   Usage check    : ai-usage"
Write-Host "   Onboarding     : maestro setup  (CLIs, Orca, remote hub)"
Write-Host "   Orca terminals : maestro orca-list ; maestro remote status"
