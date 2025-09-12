#!/usr/bin/env bash
# ===============================================
# Project: delta-backup installer
# Author: Max Haase – maxhaase@gmail.com
# ===============================================

set -euo pipefail

# ---- Paths ----
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CONF_SRC="${SCRIPT_DIR}/delta-backup.conf"         # conf shipped in the repo
CONF_DST="/etc/delta-backup.conf"                  # canonical system conf

# ---- Behavior flags ----
# Set DELTA_FORCE=1 to overwrite an existing /etc/delta-backup.conf (keeps a .bak)
DELTA_FORCE="${DELTA_FORCE:-0}"

# --- Minimal INI reader for [delta] keys from a given file ---
ini_get() {
  # Usage: ini_get FILE KEY
  local file="$1" key="$2"
  awk -v k="$key" '
    BEGIN{ IGNORECASE=1; in=0; }
    /^\s*\[/ { in = tolower($0) ~ /^\s*\[delta\]\s*$/ }
    in && /^[[:space:]]*([A-Za-z0-9_]+)[[:space:]]*=/ {
      line=$0
      sub(/[[:space:]]*#.*$/,"",line)
      split(line, a, "=")
      conf_key=a[1]; gsub(/^[ \t]+|[ \t]+$/, "", conf_key)
      if (tolower(conf_key) == tolower(k)) {
        val=a[2]; sub(/^[ \t]+/,"",val); sub(/[ \t]+$/,"",val)
        print val
        exit
      }
    }
  ' "$file"
}

# --- Step 1: Ensure the repo conf exists ---
if [[ ! -f "$CONF_SRC" ]]; then
  echo "[ERROR] Missing ${CONF_SRC}. Ensure you cloned the repo with delta-backup.conf present." >&2
  exit 2
fi

# --- Step 2: Install/Copy conf to /etc (never silently overwrite) ---
install_conf() {
  # Create /etc if needed
  install -d -m 0755 "$(dirname "$CONF_DST")"
  if [[ -f "$CONF_DST" && "$DELTA_FORCE" != "1" ]]; then
    echo "[INFO] ${CONF_DST} already exists. Not overwriting."
    echo "       To overwrite, re-run with: DELTA_FORCE=1 sudo ./delta_backup_install.sh"
  else
    if [[ -f "$CONF_DST" ]]; then
      ts="$(date +%Y%m%d-%H%M%S)"
      cp -f "$CONF_DST" "${CONF_DST}.bak-${ts}"
      echo "[INFO] Existing config backed up to ${CONF_DST}.bak-${ts}"
    fi
    install -m 0640 "$CONF_SRC" "$CONF_DST"
    echo "[INFO] Installed ${CONF_SRC} -> ${CONF_DST}"
  fi
}
install_conf

# --- Step 3: Read all required values FROM THE REPO CONF (source of truth during install) ---
DELTA_USER="$(ini_get "$CONF_SRC" delta_user)"
DELTA_GROUP="$(ini_get "$CONF_SRC" delta_group)"
BACKUP_ROOT="$(ini_get "$CONF_SRC" backup_root)"
HOST_REPO="$(ini_get "$CONF_SRC" host_repo)"
VM_REPO="$(ini_get "$CONF_SRC" vm_repo)"
HOST_PASSFILE="$(ini_get "$CONF_SRC" host_passfile)"
VM_PASSFILE="$(ini_get "$CONF_SRC" vm_passfile)"
ENGINE_BIN="$(ini_get "$CONF_SRC" engine_bin)"

# --- Step 4: Validate critical config before touching the system ---
: "${DELTA_USER:?Missing delta_user in $CONF_SRC}"
: "${DELTA_GROUP:?Missing delta_group in $CONF_SRC}"
: "${BACKUP_ROOT:?Missing backup_root in $CONF_SRC}"
: "${HOST_REPO:?Missing host_repo in $CONF_SRC}"
: "${VM_REPO:?Missing vm_repo in $CONF_SRC}"
: "${HOST_PASSFILE:?Missing host_passfile in $CONF_SRC}"
: "${VM_PASSFILE:?Missing vm_passfile in $CONF_SRC}"
: "${ENGINE_BIN:?Missing engine_bin in $CONF_SRC}"

echo "=== Install prerequisites ==="
apt update
apt install -y borgbackup python3-venv pipx acl qemu-kvm libvirt-daemon-system sudo

echo "=== Ensure user/group exist ==="
if ! id -u "$DELTA_USER" >/dev/null 2>&1; then
  adduser --disabled-password --gecos "" "$DELTA_USER"
fi
if ! getent group "$DELTA_GROUP" >/dev/null; then
  groupadd "$DELTA_GROUP"
fi
usermod -a -G "$DELTA_GROUP" "$DELTA_USER"

echo "=== Create repository directories with group access ==="
mkdir -p "$HOST_REPO" "$VM_REPO"
chown -R "$DELTA_USER:$DELTA_GROUP" "$BACKUP_ROOT"
chmod -R 2770 "$BACKUP_ROOT"   # setgid so new files inherit the group

echo "=== Prepare config directory skeleton (no secrets created) ==="
install -d -m 0750 -o "$DELTA_USER" -g "$DELTA_GROUP" "/home/$DELTA_USER/.config/delta"

echo "=== Optional UI: BorgWeb ==="
sudo -u "$DELTA_USER" pipx ensurepath || true
sudo -u "$DELTA_USER" pipx install borgweb || true

echo "=== Initialize repositories (interactive once for passphrases) ==="
sudo -u "$DELTA_USER" "$ENGINE_BIN" init --encryption=repokey-blake2 "$VM_REPO" || true
sudo -u "$DELTA_USER" "$ENGINE_BIN" init --encryption=repokey-blake2 "$HOST_REPO" || true

echo "=== Tighten passfile permissions if present ==="
if [[ -e "$HOST_PASSFILE" ]]; then
  chown "$DELTA_USER:$DELTA_GROUP" "$HOST_PASSFILE" || true
  chmod 0640 "$HOST_PASSFILE" || true
fi
if [[ -e "$VM_PASSFILE" ]]; then
  chown "$DELTA_USER:$DELTA_GROUP" "$VM_PASSFILE" || true
  chmod 0640 "$VM_PASSFILE" || true
fi

echo "=== Done ==="
echo "Config is installed at: $CONF_DST"
echo "Run the orchestrator with:"
echo "  sudo ./delta_backup.py"
echo "  # or, if testing a different conf path:"
echo "  sudo DELTA_CONFIG=\"$CONF_DST\" ./delta_backup.py"
