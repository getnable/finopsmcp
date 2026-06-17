# nable terminal demo, on-brand (graphite + ice blue).
# Render:  vhs docs/video/nable-demo.tape
# Output:  nable-demo.gif + .mp4
#
# This is the install + trust + wire-in segment (~18s). Pair with a Screen Studio
# recording of Claude answering a cost question for the full hero video.

Output docs/video/nable-demo.gif
Output docs/video/nable-demo.mp4

Set Shell "bash"
Set FontSize 19
Set FontFamily "JetBrains Mono"
Set Width 1280
Set Height 720
Set Padding 40
Set Margin 60
Set MarginFill "#0a0c0d"
Set BorderRadius 12
Set Framerate 30
Set TypingSpeed 36ms
Set CursorBlink false
Set Theme { "background": "#0d0f10", "foreground": "#eef2f4", "cursor": "#4db8d4", "selection": "#1b2024", "black": "#15191c", "red": "#ff7a70", "green": "#4db8d4", "yellow": "#febc2e", "blue": "#4db8d4", "magenta": "#aeb7bd", "cyan": "#4db8d4", "white": "#eef2f4", "brightBlack": "#6b7177", "brightRed": "#ff7a70", "brightGreen": "#7fd0e6", "brightYellow": "#febc2e", "brightBlue": "#7fd0e6", "brightMagenta": "#aeb7bd", "brightCyan": "#7fd0e6", "brightWhite": "#ffffff" }

# hidden setup: clean prompt + curated command output, no dev-machine noise
Hide
Type "source docs/video/_demo_fns.sh"
Enter
Show
Sleep 400ms

# 1. one-line install
Type "uvx --from finops-mcp finops welcome"
Sleep 500ms
Enter
Sleep 900ms

# 2. it runs locally and read-only (the trust beat)
Type "finops doctor"
Sleep 400ms
Enter
Sleep 1200ms

# 3. wire it into Claude
Type "finops setup claude"
Sleep 400ms
Enter
Sleep 1500ms

# 4. the handoff line
Type `# now ask Claude:  "what drove our AWS bill up this month?"`
Sleep 2400ms
