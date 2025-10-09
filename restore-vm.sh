#!/bin/bash
# ==============================================================
# Project: delta-backup - Restore backups created by backup.py
# Author: Max Haase – maxhaase@gmail.com
# ==============================================================

CONFIG_FILE="/etc/delta-backup.conf"

# === Function: Print error and exit ===
die() {
    echo -e "\033[1;31m[ERROR]\033[0m $*" >&2
    exit 1
}

# === Function: Parse config ===
parse_config() {
    local key="$1"
    grep -Ei "^${key}[[:space:]]*=" "$CONFIG_FILE" | sed 's/#.*//' | sed 's/^[^=]*=//' | tr -d ' '
}

# === Load configuration ===
[ -f "$CONFIG_FILE" ] || die "Config not found: $CONFIG_FILE"

VM_REPO=$(parse_config "vm_repo")
HOST_REPO=$(parse_config "host_repo")
VM_PASSFILE=$(parse_config "vm_passfile")
HOST_PASSFILE=$(parse_config "host_passfile")

[ -d "$VM_REPO" ]     || die "Missing VM repository: $VM_REPO"
[ -d "$HOST_REPO" ]   || die "Missing host repository: $HOST_REPO"
[ -f "$VM_PASSFILE" ] || die "Missing VM passfile: $VM_PASSFILE"
[ -f "$HOST_PASSFILE" ] || die "Missing host passfile: $HOST_PASSFILE"

# === Ensure fzf is installed ===
command -v fzf >/dev/null || die "'fzf' not found – please install it first (apt install fzf)"

# === Select which repo to restore ===
echo -e "\n\033[1;36mSelect the repository to restore from:\033[0m"
echo -e "\033[1;33mUse the \033[1mUP\033[0;33m and \033[1mDOWN\033[0;33m arrow keys to navigate. Press \033[1mENTER\033[0;33m to select.\033[0m"

REPO_SELECTION=$(printf "Host backup  [%s]\nVM backup    [%s]" "$HOST_REPO" "$VM_REPO" | fzf --ansi --prompt="Repository: ")

case "$REPO_SELECTION" in
  Host*)  REPO="$HOST_REPO"; PASSFILE="$HOST_PASSFILE" ;;
  VM*)    REPO="$VM_REPO";   PASSFILE="$VM_PASSFILE" ;;
  *)      die "No repository selected" ;;
esac

# === Select archive ===
export BORG_PASSCOMMAND="cat $PASSFILE"

mapfile -t ARCHIVES < <(borg list "$REPO" --format '{archive}{NL}' 2>/dev/null)
[ ${#ARCHIVES[@]} -eq 0 ] && die "No archives found in $REPO"

echo -e "\n\033[1;36mSelect archive to restore:\033[0m"
echo -e "\033[1;33mUse arrow keys to navigate. Press ENTER to select.\033[0m"

ARCHIVE=$(printf "%s\n" "${ARCHIVES[@]}" | fzf --ansi --prompt="Archive: ")
[ -n "$ARCHIVE" ] || die "No archive selected"

# === Select extract destination ===
echo -e "\n\033[1;36mChoose extract location:\033[0m"
read -rp "Target directory (default: current dir): " TARGET
TARGET="${TARGET:-.}"
mkdir -p "$TARGET" || die "Cannot create $TARGET"

# === Extract ===
echo -e "\n\033[1;32m[INFO]\033[0m Extracting archive: $ARCHIVE"
echo -e "\033[1;32m[INFO]\033[0m Target directory: $TARGET"

borg extract "$REPO::$ARCHIVE" --progress --verbose --show-rc --filter=AME --strip-components 1 -C "$TARGET"
RC=$?

if [[ $RC -eq 0 ]]; then
    echo -e "\n\033[1;32m[SUCCESS]\033[0m Archive extracted to: $TARGET"
else
    echo -e "\n\033[1;31m[FAILURE]\033[0m borg extract exited with code $RC"
fi
