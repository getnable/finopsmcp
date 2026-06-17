# Hidden setup for the VHS demo. Overrides commands with shell functions so the
# visible scene shows real-looking commands with clean, curated output and no
# "command not found" noise. Ice-blue "ok" via ANSI truecolor (#4db8d4).
PS1='$ '
_ok=$'\033[38;2;77;184;212mok\033[0m'

pip() { printf 'Successfully installed finops-mcp-0.8.77\n'; }

finops() {
  case "$1 $2" in
    "doctor"*|"doctor "*)
      printf '\n  nable - finops-mcp doctor\n'
      printf '  ---------------------------------------------\n'
      printf '  %s   Credentials in OS keychain (not plaintext)\n' "$_ok"
      printf '  %s   AWS key is read-only, no write permissions\n' "$_ok"
      printf '  %s   Queries go straight to your cloud, nothing leaves this machine\n\n' "$_ok"
      ;;
    "setup claude")
      printf '  %s   Added nable to Claude Desktop.\n' "$_ok"
      printf '  %s   Restart Claude, then just ask about your costs.\n' "$_ok"
      ;;
  esac
}
clear
