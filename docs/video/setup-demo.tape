# nable finops-mcp — setup demo
# Render with: vhs setup-demo.tape
# Output: setup-demo.mp4 + setup-demo.gif
#
# Install VHS: brew install charmbracelet/tap/vhs
# Requires: ffmpeg  (brew install ffmpeg)

# ── Output ──────────────────────────────────────────────────────────────────
Output setup-demo.mp4
Output setup-demo.gif

# ── Terminal appearance ──────────────────────────────────────────────────────
Set Shell "bash"
Set FontSize 14
Set FontFamily "JetBrains Mono"
Set Width 1200
Set Height 680
Set Theme "Catppuccin Mocha"
Set Padding 24
Set Framerate 30
Set PlaybackSpeed 1.0
Set CursorBlink false
Set TypingSpeed 40ms

# ── Scene 1: install ─────────────────────────────────────────────────────────
# Clean prompt
Type "# Step 1 — install"
Sleep 600ms
Enter
Sleep 400ms

Type "pip install finops-mcp"
Sleep 800ms
Enter

# Simulate pip output (fast-forward feel)
Sleep 200ms
Type "Collecting finops-mcp"
Enter
Sleep 100ms
Type "  Downloading finops_mcp-0.8.25-py3-none-any.whl (124 kB)"
Enter
Sleep 100ms
Type "Collecting boto3>=1.34.0"
Enter
Sleep 80ms
Type "Collecting mcp[cli]>=1.3.0"
Enter
Sleep 80ms
Type "Collecting sqlalchemy>=2.0.0"
Enter
Sleep 600ms
Type "Successfully installed finops-mcp-0.8.25"
Enter
Sleep 1200ms

# ── Scene 2: verify ──────────────────────────────────────────────────────────
Type "finops --version"
Sleep 600ms
Enter
Sleep 300ms
Type "finops-mcp 0.8.25"
Enter
Sleep 1000ms

# ── Scene 3: check AWS credentials ──────────────────────────────────────────
Type "# Step 2 — verify AWS credentials"
Enter
Sleep 400ms

Type "aws sts get-caller-identity"
Sleep 700ms
Enter
Sleep 500ms

Type '{'
Enter
Type '    "UserId": "AIDA4EXAMPLE3USER",'
Enter
Type '    "Account": "123456789012",'
Enter
Type '    "Arn": "arn:aws:iam::123456789012:user/finops-admin"'
Enter
Type '}'
Enter
Sleep 1400ms

# ── Scene 4: deploy CUR stack ────────────────────────────────────────────────
Type "# Step 3 — deploy Cost and Usage Report pipeline"
Enter
Sleep 400ms

Type "finops setup aws-cur"
Sleep 800ms
Enter
Sleep 600ms

# Output header
Type "  AWS — Cost and Usage Report (CUR) pipeline"
Enter
Type "  ✓  AWS credentials valid — account 123456789012 (finops-admin)"
Enter
Sleep 800ms

Type "  Creating stack (takes ~2 minutes)..."
Enter
Sleep 500ms

# Simulate dots appearing as CF deploys
Type "  ."
Sleep 400ms
Type "."
Sleep 400ms
Type "."
Sleep 400ms
Type "."
Sleep 400ms
Type "."
Sleep 400ms
Type "."
Sleep 400ms
Type "."
Sleep 400ms
Type "."
Sleep 400ms
Type "."
Sleep 400ms
Type "."
Enter
Sleep 400ms

Type "  ✓  Stack deployed successfully (CREATE_COMPLETE)"
Enter
Sleep 800ms

# Show outputs
Type "  ✓  CUR pipeline ready. Add these to your config:"
Enter
Sleep 400ms
Type "       CUR_S3_BUCKET=nable-cur-123456789012"
Enter
Sleep 100ms
Type "       CUR_ATHENA_DATABASE=nable_cur"
Enter
Sleep 100ms
Type "       CUR_ATHENA_TABLE=nable-cur"
Enter
Sleep 100ms
Type "       CUR_ATHENA_RESULTS_BUCKET=nable-cur-123456789012"
Enter
Sleep 100ms
Type "       CUR_ATHENA_WORKGROUP=nable-cur"
Enter
Sleep 600ms

Type "  !  First CUR data arrives within 24 hours."
Enter
Sleep 800ms

# Prompt to store
Type "  Store these in the nable credential vault? [Y/n]: "
Sleep 600ms
Type "y"
Enter
Sleep 400ms

Type "  ✓  Stored in vault. CUR line-item queries are now available."
Enter
Type "  ✓  Restart Claude Desktop to pick up the new env vars."
Enter
Sleep 1800ms

# ── Scene 5: configure Claude Desktop ───────────────────────────────────────
Type "# Step 4 — add to Claude Desktop"
Enter
Sleep 400ms

Type "finops setup claude"
Sleep 700ms
Enter
Sleep 500ms

Type '  ✓  Config written to ~/Library/Application Support/Claude/claude_desktop_config.json'
Enter
Type "  ✓  Restart Claude Desktop and start asking questions."
Enter
Sleep 2000ms

# ── Final pause before loop ──────────────────────────────────────────────────
Sleep 1000ms
