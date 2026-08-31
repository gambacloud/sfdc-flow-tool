#!/usr/bin/env bash
# Installs sfdc-flow-forge's MCP server for Claude Desktop on macOS/Linux.
# Downloaded from the web UI's Options panel - run it yourself, it does not
# run itself. Every mutating step is previewed and confirmed below.
set -euo pipefail

REPO_URL="git+https://github.com/gambacloud/sfdc-flow-tool.git"
VENV_DIR="$HOME/.sfdc-flow-forge/venv"

if [[ "$(uname)" == "Darwin" ]]; then
    CONFIG_PATH="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
else
    CONFIG_PATH="$HOME/.config/Claude/claude_desktop_config.json"
fi

echo "This installer will:"
echo "  1. Check for Python 3.10+"
echo "  2. Create/upgrade a venv at $VENV_DIR"
echo "  3. pip install sfdc-flow-forge from GitHub into that venv (isolated - not system Python)"
echo "  4. Ask for your own Gemini or Anthropic API key (kept local, never sent anywhere but into the config file below)"
echo "  5. Optionally install Salesforce CLI"
echo "  6. Merge an mcpServers.sfdc-flow-forge entry into:"
echo "     $CONFIG_PATH"
echo
read -rp "Continue? [Y/n] " ok
if [[ "$ok" =~ ^[Nn] ]]; then
    exit 0
fi

PY="$(command -v python3 || command -v python || true)"
if [[ -z "$PY" ]]; then
    echo "Python not found. Install Python 3.10+ from https://www.python.org/downloads/ and re-run this script."
    exit 1
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "Python 3.10+ required. Install it from https://www.python.org/downloads/ and re-run this script."
    exit 1
fi

if [[ ! -d "$VENV_DIR" ]]; then
    echo "Creating venv at $VENV_DIR ..."
    "$PY" -m venv "$VENV_DIR"
fi
VENV_PY="$VENV_DIR/bin/python"

echo "Installing sfdc-flow-forge (this can take a minute) ..."
if ! "$VENV_PY" -m pip install --upgrade "$REPO_URL"; then
    echo "pip install failed - see the output above."
    exit 1
fi

echo
echo "Choose your LLM provider:"
echo "  1) Gemini (GEMINI_API_KEY)"
echo "  2) Anthropic (ANTHROPIC_API_KEY)"
read -rp "Enter 1 or 2: " choice
if [[ "$choice" == "2" ]]; then
    ENV_VAR_NAME="ANTHROPIC_API_KEY"
else
    ENV_VAR_NAME="GEMINI_API_KEY"
fi
read -rsp "Paste your $ENV_VAR_NAME (input hidden): " API_KEY
echo
if [[ -z "$API_KEY" ]]; then
    echo "No key entered - aborting."
    exit 1
fi

echo
read -rp "Install Salesforce CLI now? [y/N] " install_sf
if [[ "$install_sf" =~ ^[Yy] ]]; then
    if command -v sf >/dev/null 2>&1; then
        echo "sf is already on PATH - skipping."
    elif command -v npm >/dev/null 2>&1; then
        npm install --global @salesforce/cli
    else
        echo "npm not found. Install Salesforce CLI manually: https://developer.salesforce.com/tools/salesforcecli"
    fi
fi

CONFIG_DIR="$(dirname "$CONFIG_PATH")"
mkdir -p "$CONFIG_DIR"

echo
echo "Updating $CONFIG_PATH ..."
"$VENV_PY" - "$CONFIG_PATH" "$VENV_PY" "$ENV_VAR_NAME" "$API_KEY" <<'PYEOF'
import json
import os
import sys

config_path, venv_py, env_var, api_key = sys.argv[1:5]

config = {}
if os.path.exists(config_path):
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

config.setdefault("mcpServers", {})
existing = config["mcpServers"].get("sfdc-flow-forge")
if existing is not None:
    print("An mcpServers.sfdc-flow-forge entry already exists:")
    print(json.dumps(existing, indent=2))
    resp = input("Overwrite it? [y/N] ")
    if resp.strip().lower() != "y":
        print("Leaving the existing entry untouched.")
        sys.exit(0)

config["mcpServers"]["sfdc-flow-forge"] = {
    "command": venv_py,
    "args": ["-m", "mcp_server"],
    "env": {env_var: api_key},
}

with open(config_path, "w", encoding="utf-8") as f:
    json.dump(config, f, indent=2)

print(f"Wrote {config_path}")
PYEOF

echo
echo "Done. Installed:"
echo "  - venv: $VENV_DIR"
echo "  - sfdc-flow-forge package (latest from GitHub)"
echo "  - Claude Desktop MCP server entry: sfdc-flow-forge"
echo
echo "Restart Claude Desktop for this to take effect."
echo "Before validate/deploy tools will work, you still need to run:"
echo "  sf org login web --alias <alias>"
echo "(build/approve/revise work without an org connection)"
