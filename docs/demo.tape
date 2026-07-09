# Renders docs/demo.gif, the animated terminal demo embedded in the README
# (GitHub + PyPI both play it). Re-render after output changes:  vhs docs/demo.tape
# The hidden setup aliases uvx to the local venv so the recording is instant; the
# typed command is exactly what a real user runs and the output is identical.

Output docs/demo.gif

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
# arch -arm64: vhs/ttyd are x86_64 Homebrew builds, so without it the venv python
# runs under Rosetta and pydantic_core fails to import, silently dropping the
# value-moment scan (the repo's known venv-arch gotcha).
Type `uvx() { NABLE_NO_TELEMETRY=1 FINOPS_NO_KEYRING=1 arch -arm64 /Users/chandan/finops/.venv/bin/finops "${@:2}"; }`
Enter
Type "clear"
Enter
Show

Sleep 800ms
Type "uvx nable welcome --demo"
Sleep 600ms
Enter
Sleep 12s
