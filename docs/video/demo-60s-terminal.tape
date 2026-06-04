# nable - 60-second demo, TERMINAL segment (~18s of the final cut)
#
# This is the install/trust segment. Pair it with a screen recording of Claude
# Desktop answering a cost question (see demo-runbook.md) for the full 60s video.
#
# Render:  vhs demo-60s-terminal.tape
# Install: brew install charmbracelet/tap/vhs ffmpeg
# Output:  demo-60s-terminal.mp4 + .gif

Output demo-60s-terminal.mp4
Output demo-60s-terminal.gif

Set Shell "bash"
Set FontSize 18
Set FontFamily "JetBrains Mono"
Set Width 1280
Set Height 720
Set Theme "Catppuccin Mocha"
Set Padding 28
Set Framerate 30
Set TypingSpeed 38ms
Set CursorBlink false

# ── 1. one-line install ──────────────────────────────────────────────────────
Type "pip install finops-mcp"
Sleep 500ms
Enter
Sleep 200ms
Type "Successfully installed finops-mcp-0.8.45"
Enter
Sleep 900ms

# ── 2. it runs locally and read-only (the trust beat) ────────────────────────
Type "finops doctor"
Sleep 400ms
Enter
Sleep 300ms
Type "  nable · finops-mcp doctor"
Enter
Type "  ─────────────────────────────────────────────"
Enter
Sleep 120ms
Type "  ✓  Credentials in OS keychain (not plaintext)"
Enter
Sleep 120ms
Type "  ✓  AWS key is read-only - no write permissions"
Enter
Sleep 120ms
Type "  ✓  Queries go straight to your cloud - nothing leaves this machine"
Enter
Sleep 1000ms

# ── 3. wire it into Claude ───────────────────────────────────────────────────
Type "finops setup claude"
Sleep 400ms
Enter
Sleep 250ms
Type "  ✓  Added nable to Claude Desktop."
Enter
Type "  ✓  Restart Claude and just ask about your costs."
Enter
Sleep 1600ms

# ── 4. the handoff line ──────────────────────────────────────────────────────
Type "# now ask Claude:  \"what drove our AWS bill up this month?\""
Sleep 2200ms
