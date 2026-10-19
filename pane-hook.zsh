# claude-session-restore: record this iTerm2 pane's id + cwd so snapshots can see idle shells.
# (macOS hides login-shell environments from `ps`, so panes report themselves.)
if [[ -n "$ITERM_SESSION_ID" && -o interactive ]]; then
  _csr_pane_dir="$HOME/.local/state/claude-session-restore/panes"
  _csr_record_pane() {
    [[ "$PWD" == "$_csr_last_pwd" ]] && return
    _csr_last_pwd="$PWD"
    mkdir -p "$_csr_pane_dir" 2>/dev/null
    print -r -- "$$	$ITERM_SESSION_ID	${ITERM_PROFILE}	$PWD" >| "$_csr_pane_dir/${ITERM_SESSION_ID#*:}" 2>/dev/null
  }
  autoload -Uz add-zsh-hook
  add-zsh-hook precmd _csr_record_pane
fi
