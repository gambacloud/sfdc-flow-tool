# Installs sfdc-flow-forge's MCP server for Claude Desktop on Windows.
# Downloaded from the web UI's Options panel - run it yourself, it does not
# run itself. Every mutating step is previewed and confirmed below.

$ErrorActionPreference = "Stop"

$RepoUrl = "git+https://github.com/gambacloud/sfdc-flow-tool.git"
$VenvDir = Join-Path $env:USERPROFILE ".sfdc-flow-forge\venv"
$ConfigPath = Join-Path $env:APPDATA "Claude\claude_desktop_config.json"

function ConvertTo-HashtableDeep {
    param($InputObject)
    if ($null -eq $InputObject) { return $null }
    if ($InputObject -is [System.Management.Automation.PSCustomObject]) {
        $hash = @{}
        foreach ($prop in $InputObject.PSObject.Properties) {
            $hash[$prop.Name] = ConvertTo-HashtableDeep $prop.Value
        }
        return $hash
    }
    if ($InputObject -is [System.Collections.IEnumerable] -and $InputObject -isnot [string]) {
        $arr = @()
        foreach ($item in $InputObject) { $arr += , (ConvertTo-HashtableDeep $item) }
        return , $arr
    }
    return $InputObject
}

Write-Host "This installer will:"
Write-Host "  1. Check for Python 3.10+"
Write-Host "  2. Create/upgrade a venv at $VenvDir"
Write-Host "  3. pip install sfdc-flow-forge from GitHub into that venv (isolated - not system Python)"
Write-Host "  4. Ask for your own Gemini or Anthropic API key (kept local, never sent anywhere but into the config file below)"
Write-Host "  5. Optionally install Salesforce CLI"
Write-Host "  6. Merge an mcpServers.sfdc-flow-forge entry into:"
Write-Host "     $ConfigPath"
Write-Host ""
$ok = Read-Host "Continue? [Y/n]"
if ($ok -match '^[Nn]') {
    exit 0
}

$pyCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pyCmd) {
    Write-Host "Python not found. Install Python 3.10+ from https://www.python.org/downloads/ and re-run this script."
    exit 1
}
& python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Python 3.10+ required. Install it from https://www.python.org/downloads/ and re-run this script."
    exit 1
}

if (-not (Test-Path $VenvDir)) {
    Write-Host "Creating venv at $VenvDir ..."
    python -m venv $VenvDir
}
$VenvPy = Join-Path $VenvDir "Scripts\python.exe"

Write-Host "Installing sfdc-flow-forge (this can take a minute) ..."
& $VenvPy -m pip install --upgrade $RepoUrl
if ($LASTEXITCODE -ne 0) {
    Write-Host "pip install failed - see the output above."
    exit 1
}

Write-Host ""
Write-Host "Choose your LLM provider:"
Write-Host "  1) Gemini (GEMINI_API_KEY)"
Write-Host "  2) Anthropic (ANTHROPIC_API_KEY)"
$choice = Read-Host "Enter 1 or 2"
if ($choice -eq '2') {
    $EnvVarName = "ANTHROPIC_API_KEY"
} else {
    $EnvVarName = "GEMINI_API_KEY"
}
$secure = Read-Host "Paste your $EnvVarName (input hidden)" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
$ApiKey = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
if ([string]::IsNullOrWhiteSpace($ApiKey)) {
    Write-Host "No key entered - aborting."
    exit 1
}

Write-Host ""
$installSf = Read-Host "Install Salesforce CLI now? [y/N]"
if ($installSf -match '^[Yy]') {
    $sfCmd = Get-Command sf -ErrorAction SilentlyContinue
    if ($sfCmd) {
        Write-Host "sf is already on PATH - skipping."
    } else {
        $npmCmd = Get-Command npm -ErrorAction SilentlyContinue
        if ($npmCmd) {
            npm install --global "@salesforce/cli"
        } else {
            Write-Host "npm not found. Install Salesforce CLI manually: https://developer.salesforce.com/tools/salesforcecli"
        }
    }
}

$configDir = Split-Path $ConfigPath
if (-not (Test-Path $configDir)) {
    New-Item -ItemType Directory -Force -Path $configDir | Out-Null
}

$config = @{}
if (Test-Path $ConfigPath) {
    $raw = Get-Content $ConfigPath -Raw
    if ($raw -and $raw.Trim().Length -gt 0) {
        $parsed = $raw | ConvertFrom-Json
        $config = ConvertTo-HashtableDeep $parsed
    }
}
if (-not $config.ContainsKey("mcpServers")) {
    $config["mcpServers"] = @{}
}

$skipMerge = $false
if ($config["mcpServers"].ContainsKey("sfdc-flow-forge")) {
    Write-Host "An mcpServers.sfdc-flow-forge entry already exists:"
    Write-Host ($config["mcpServers"]["sfdc-flow-forge"] | ConvertTo-Json -Depth 10)
    $overwrite = Read-Host "Overwrite it? [y/N]"
    if ($overwrite -notmatch '^[Yy]') {
        Write-Host "Leaving the existing entry untouched."
        $skipMerge = $true
    }
}

if (-not $skipMerge) {
    $config["mcpServers"]["sfdc-flow-forge"] = @{
        command = $VenvPy
        args    = @("-m", "mcp_server")
        env     = @{ $EnvVarName = $ApiKey }
    }
    ($config | ConvertTo-Json -Depth 10) | Set-Content -Path $ConfigPath -Encoding utf8
    Write-Host "Wrote $ConfigPath"
}

Write-Host ""
Write-Host "Done. Installed:"
Write-Host "  - venv: $VenvDir"
Write-Host "  - sfdc-flow-forge package (latest from GitHub)"
Write-Host "  - Claude Desktop MCP server entry: sfdc-flow-forge"
Write-Host ""
Write-Host "Restart Claude Desktop for this to take effect."
Write-Host "Before validate/deploy tools will work, you still need to run:"
Write-Host "  sf org login web --alias <alias>"
Write-Host "(build/approve/revise work without an org connection)"
