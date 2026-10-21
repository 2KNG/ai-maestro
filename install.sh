#!/usr/bin/env bash
# ==============================================================================
# ai-maestro — One-Shot Installer & Live Symlink Manager
# Repository: https://github.com/2KNG/ai-maestro
# License: MIT
#
# Usage:
#   1) One-liner from anywhere:
#      curl -fsSL https://raw.githubusercontent.com/2KNG/ai-maestro/main/install.sh | bash
#   2) Local clone:
#      git clone https://github.com/2KNG/ai-maestro.git ~/dev/ai-maestro && ~/dev/ai-maestro/install.sh
#
# After installing, runs the 3-step onboarding (`maestro setup`):
#   [1/3] Antigravity / Codex / Claude CLIs  [2/3] Orca  [3/3] remote hub address
# Arguments are passed to `maestro setup`, e.g.:
#   ./install.sh --yes --hub <your-server-domain-or-ip>
#   curl -fsSL .../install.sh | bash -s -- --check-only
# Set MAESTRO_SETUP=0 to skip onboarding.
# ==============================================================================
set -euo pipefail

REPO_URL="https://github.com/2KNG/ai-maestro.git"
# Prefer the branded install override; retain the legacy environment fallback.
DEFAULT_INSTALL_DIR="${MAESTRO_DIR:-${AGY_ORCA_DIR:-$HOME/dev/ai-maestro}}"

# Determine if running from inside a cloned repo or via curl|bash
SCRIPT_SOURCE="${BASH_SOURCE[0]:-}"
if [[ -n "$SCRIPT_SOURCE" && -f "$SCRIPT_SOURCE" ]]; then
   candidate_dir="$(cd "$(dirname "$SCRIPT_SOURCE")" && pwd)"
  if [[ -d "$candidate_dir/skills/multi-agent-orchestrator" ]]; then
    INSTALL_DIR="$candidate_dir"
  else
    INSTALL_DIR="$DEFAULT_INSTALL_DIR"
  fi
else
  INSTALL_DIR="$DEFAULT_INSTALL_DIR"
fi

echo "🚀 [ai-maestro] Installing / Syncing Multi-Agent Orchestrator & AOH Live Widget..."
echo "   📂 Target Repo Path: $INSTALL_DIR"

mkdir -p "$(dirname "$INSTALL_DIR")"
if [[ ! -d "$INSTALL_DIR/.git" ]]; then
  if [[ -d "$INSTALL_DIR" && -d "$INSTALL_DIR/skills/multi-agent-orchestrator" ]]; then
    echo "   ✓ Using existing directory at $INSTALL_DIR"
  else
    echo "   ⬇ Cloning $REPO_URL -> $INSTALL_DIR ..."
    rm -rf "$INSTALL_DIR"
    git clone "$REPO_URL" "$INSTALL_DIR"
  fi
else
  echo "   ✓ Git repository detected at $INSTALL_DIR"
fi

SCRIPTS_DIR="$INSTALL_DIR/skills/multi-agent-orchestrator/scripts"
chmod +x "$SCRIPTS_DIR"/*.py || true

# Preserve and validate existing Codex settings before any installation changes.
# CODEX_HOME is honored; missing files stay missing and invalid files fail closed.
python3 "$SCRIPTS_DIR/codex_config_safe.py"

# 1. Create portable CLI wrappers in repo bin/
mkdir -p "$INSTALL_DIR/bin"

cat > "$INSTALL_DIR/bin/maestro" <<EOF
#!/usr/bin/env bash
exec python3 "$SCRIPTS_DIR/maestro.py" "\$@"
EOF
# Silent backward-compatible alias (legacy command name) -> maestro
cp -a "$INSTALL_DIR/bin/maestro" "$INSTALL_DIR/bin/agy-orca"

cat > "$INSTALL_DIR/bin/ai-usage" <<EOF
#!/usr/bin/env bash
exec python3 "$SCRIPTS_DIR/check_all_usage.py" "\$@"
EOF

cat > "$INSTALL_DIR/bin/ai-usage-widget" <<EOF
#!/usr/bin/env bash
exec python3 "$SCRIPTS_DIR/render_usage_widget.py" "\$@"
EOF

cat > "$INSTALL_DIR/bin/ai-usage-web" <<EOF
#!/usr/bin/env bash
URL="http://127.0.0.1:18765/"
if command -v xdg-open >/dev/null 2>&1; then
  xdg-open "\$URL" >/dev/null 2>&1 &
elif command -v open >/dev/null 2>&1; then
  open "\$URL"
else
  echo "\$URL"
fi
EOF

chmod +x "$INSTALL_DIR/bin/"*

# 2. Symlink ~/.local/bin executables
mkdir -p "$HOME/.local/bin"
for bin_name in maestro agy-orca ai-usage ai-usage-widget ai-usage-web; do
  ln -sfn "$INSTALL_DIR/bin/$bin_name" "$HOME/.local/bin/$bin_name"
done
echo "   ✓ Linked CLI commands in ~/.local/bin/{maestro,ai-usage,ai-usage-widget,ai-usage-web}"

# 3. Symlink Antigravity skills & plugins directly to repo for continuous Git management
mkdir -p "$HOME/.gemini/config/skills" "$HOME/.gemini/config/plugins" "$HOME/.gemini/antigravity/skills"

for skill_target in "$HOME/.gemini/config/skills/multi-agent-orchestrator" "$HOME/.gemini/antigravity/skills/multi-agent-orchestrator"; do
  if [[ -d "$skill_target" && ! -L "$skill_target" ]]; then
    rm -rf "$skill_target"
  fi
  ln -sfn "$INSTALL_DIR/skills/multi-agent-orchestrator" "$skill_target"
done

if [[ -d "$HOME/.gemini/config/plugins/codex-claude-bridge" && ! -L "$HOME/.gemini/config/plugins/codex-claude-bridge" ]]; then
  rm -rf "$HOME/.gemini/config/plugins/codex-claude-bridge"
fi
ln -sfn "$INSTALL_DIR/plugins/codex-claude-bridge" "$HOME/.gemini/config/plugins/codex-claude-bridge"

for sub_skill in codex claude; do
  if [[ -d "$INSTALL_DIR/plugins/codex-claude-bridge/skills/$sub_skill" ]]; then
    for dest_root in "$HOME/.gemini/config/skills" "$HOME/.gemini/antigravity/skills"; do
      if [[ -d "$dest_root/$sub_skill" && ! -L "$dest_root/$sub_skill" ]]; then
        rm -rf "$dest_root/$sub_skill"
      fi
      ln -sfn "$INSTALL_DIR/plugins/codex-claude-bridge/skills/$sub_skill" "$dest_root/$sub_skill"
    done
  fi
done
echo "   ✓ Linked Antigravity skills & codex-claude-bridge plugin to $INSTALL_DIR"

# 4. Install global agent rules (rewriting the __HOME__ token -> current $HOME)
mkdir -p "$HOME/.gemini" "$HOME/.codex" "$HOME/.claude" "$HOME/dev"
sed "s|__HOME__|$HOME|g" "$INSTALL_DIR/rules/GEMINI.md" > "$HOME/.gemini/GEMINI.md"
sed "s|__HOME__|$HOME|g" "$INSTALL_DIR/rules/AGENTS.workspace.md" > "$HOME/dev/AGENTS.md"
sed "s|__HOME__|$HOME|g" "$INSTALL_DIR/rules/AGENTS.codex.md" > "$HOME/.codex/AGENTS.md"
sed "s|__HOME__|$HOME|g" "$INSTALL_DIR/rules/CLAUDE.md" > "$HOME/.claude/CLAUDE.md"
echo "   ✓ Installed global rules (~/.gemini/GEMINI.md, ~/dev/AGENTS.md, ~/.codex/AGENTS.md, ~/.claude/CLAUDE.md)"

# 5. Configure & start systemd user service for the live widget bridge daemon (port 18765)
mkdir -p "$HOME/.config/systemd/user" "$INSTALL_DIR/systemd"
cat > "$INSTALL_DIR/systemd/agy-widget-bridge.service" <<EOF
[Unit]
Description=Antigravity Multi-Agent Usage Widget HTTP Bridge Daemon (Port 18765)
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/env python3 $SCRIPTS_DIR/widget_bridge_daemon.py
Restart=always
RestartSec=2
Environment=PYTHONUNBUFFERED=1
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=default.target
EOF

ln -sfn "$INSTALL_DIR/systemd/agy-widget-bridge.service" "$HOME/.config/systemd/user/agy-widget-bridge.service"

if command -v systemctl >/dev/null 2>&1 && systemctl --user status >/dev/null 2>&1; then
  systemctl --user daemon-reload
  systemctl --user enable --now agy-widget-bridge.service
  systemctl --user restart agy-widget-bridge.service
  echo "   ✓ Started systemd service: agy-widget-bridge.service (http://127.0.0.1:18765)"
else
  pkill -f "widget_bridge_daemon.py" >/dev/null 2>&1 || true
  nohup python3 "$SCRIPTS_DIR/widget_bridge_daemon.py" >/tmp/agy-widget-bridge.log 2>&1 &
  echo "   ✓ Started background daemon: widget_bridge_daemon.py (http://127.0.0.1:18765)"
fi

# 6. Onboarding: core CLIs -> optional local Orca -> optional remote hub (maestro setup)
if [[ "${MAESTRO_SETUP:-1}" != "0" ]]; then
  if [[ -t 0 ]]; then
    python3 "$SCRIPTS_DIR/maestro.py" setup "$@" || echo "   ⚠ maestro setup did not finish; rerun: maestro setup"
  elif (exec </dev/tty) 2>/dev/null; then
    # curl | bash: stdin is the script itself, so prompt on the terminal.
    python3 "$SCRIPTS_DIR/maestro.py" setup "$@" </dev/tty || echo "   ⚠ maestro setup did not finish; rerun: maestro setup"
  else
    python3 "$SCRIPTS_DIR/maestro.py" setup --check-only "$@" || true
    echo "   ℹ No terminal for prompts. Run 'maestro setup' for local orchestrator mode (or pass --yes --hub <address>) to add a remote hub."
  fi
fi

echo ""
echo "✅ [ai-maestro] Installation Complete!"
echo "   • Live Dashboard : http://127.0.0.1:18765"
echo "   • CLI Usage Check: ~/.local/bin/ai-usage"
echo "   • Auto Worker CLI: ~/.local/bin/maestro worker-start --pool auto --difficulty high --cwd \"\$PWD\" --wait --spec \"...\""
echo "   • Onboarding     : maestro setup  (CLIs · optional Orca · optional remote hub)"
echo "   • Orca terminals : maestro orca-list · maestro remote status"
