# Renders docs/guard-demo.gif: the agent guard installed into a project, then
# asking on an ~$128k/mo GPU launch and on a terraform destroy. No cloud account
# is needed for any of it. Re-render after the guard's output changes:
#   NABLE_VENV=/path/to/your/.venv vhs docs/guard-demo.tape
# Same trick as docs/demo.tape: the hidden setup aliases uvx to a local venv so
# the recording is instant; the typed commands are exactly what a real user runs
# and the output is identical. NABLE_VENV defaults to ~/finops/.venv, which is
# where the maintainer's checkout keeps its venv; point it at yours.

Output docs/guard-demo.gif

Set Shell bash
Set FontSize 17
# JetBrains Mono is the DESIGN.md-sanctioned mono fallback and ships with vhs;
# Geist Mono is not installed system-wide and fell back to a letter-spaced mess.
Set FontFamily "JetBrains Mono"
Set Width 1000
Set Height 640
Set Padding 24
Set TypingSpeed 55ms
Set Theme { "name": "nable", "background": "#0d0f10", "foreground": "#f4f6f7", "cursor": "#4db8d4", "black": "#15181a", "red": "#e06c75", "green": "#4db8d4", "yellow": "#e5c07b", "blue": "#61afef", "magenta": "#c678dd", "cyan": "#4db8d4", "white": "#f4f6f7", "brightBlack": "#6b7479", "brightRed": "#e06c75", "brightGreen": "#4db8d4", "brightYellow": "#e5c07b", "brightBlue": "#61afef", "brightMagenta": "#c678dd", "brightCyan": "#4db8d4", "brightWhite": "#ffffff" }

Hide
# arch -arm64 only on Apple silicon: vhs/ttyd there are x86_64 Homebrew builds,
# so without it the venv python runs under Rosetta and pydantic_core fails to
# import (the repo's known venv-arch gotcha). Elsewhere _arch stays empty.
Type `_arch=""; [ "$(uname -sm)" = "Darwin arm64" ] && _arch="arch -arm64"`
Enter
Type `uvx() { NABLE_NO_TELEMETRY=1 NABLE_NO_UPDATE_CHECK=1 FINOPS_NO_KEYRING=1 $_arch "${NABLE_VENV:-$HOME/finops/.venv}/bin/finops" "${@:2}"; }`
Enter
# A fresh project, so install writes a new .claude/settings.json every render.
Type "rm -rf /tmp/demo-app && mkdir -p /tmp/demo-app && cd /tmp/demo-app"
Enter
Type "clear"
Enter
Show

Sleep 800ms
Type "uvx nable guard install"
Sleep 600ms
Enter
Sleep 5s
Type "clear"
Enter

Sleep 500ms
Type `uvx nable guard check --command "aws ec2 run-instances --instance-type p4d.24xlarge --count 8"`
Sleep 600ms
Enter
Sleep 6s

Type `uvx nable guard check --command "terraform destroy -auto-approve"`
Sleep 600ms
Enter
Sleep 6s
