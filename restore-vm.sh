#!/usr/bin/env bash
set -euo pipefail

# ===============================================
# delta-restore-vm.sh — Restore a single VM from delta-backup
# Reads config from /etc/delta_backup.conf [delta]
# Author: Max Haase – maxhaase@gmail.com
# ===============================================

CONF_FILE="${DELTA_CONF:-/etc/delta_backup.conf}"

die()  { echo "[ERROR] $*" >&2; exit 1; }
warn() { echo "[WARN]  $*" >&2; }
info() { echo "[INFO]  $*"; }

need_cmd() { command -v "$1" >/dev/null 2>&1 || die "Missing command: $1"; }

usage() {
  cat >&2 <<EOF
Usage: $0 [--config /path/to/delta_backup.conf]

Environment:
  DELTA_CONF=/etc/delta_backup.conf   (default)

This restores a VM from the VM repository defined in the [delta] section.
EOF
  exit 1
}

# ----------------------------
# Parse args
# ----------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) shift; [[ $# -gt 0 ]] || usage; CONF_FILE="$1"; shift ;;
    -h|--help) usage ;;
    *) usage ;;
  endesac
done

# ----------------------------
# Tiny INI parser for [delta]
# ----------------------------
trim() { sed -e 's/^[[:space:]]\+//' -e 's/[[:space:]]\+$//'; }

boolify() {
  local v="$(echo -n "$1" | tr '[:upper:]' '[:lower:]')"
  case "$v" in
    1|y|yes|true|on) echo 1 ;;
    0|n|no|false|off|"") echo 0 ;;
    *) echo 0 ;;
  esac
}

declare \
  CFG_backup_root="" \
  CFG_host_repo="" \
  CFG_vm_repo="" \
  CFG_host_passfile="" \
  CFG_vm_passfile="" \
  CFG_require_mountpoint="0" \
  CFG_lock_wait="120"

[[ -r "$CONF_FILE" ]] || die "Config not readable: $CONF_FILE"

# Read only the [delta] section; strip comments; capture key=val
while IFS='=' read -r k v; do
  [[ -z "${k:-}" ]] && continue
  case "$k" in
    \#*|';'*) continue ;;
  esac
  k="$(echo "$k"   | sed 's/[[:space:]]\+$//' | sed 's/^[[:space:]]\+//' )"
  v="$(echo "$v"   | sed 's/#.*$//'         | trim)"
  case "$k" in
    backup_root)          CFG_backup_root="$v" ;;
    host_repo)            CFG_host_repo="$v" ;;
    vm_repo)              CFG_vm_repo="$v" ;;
    host_passfile)        CFG_host_passfile="$v" ;;
    vm_passfile)          CFG_vm_passfile="$v" ;;
    require_mountpoint)   CFG_require_mountpoint="$(boolify "$v")" ;;
    lock_wait)            CFG_lock_wait="$v" ;;
  esac
done < <(
  awk '
    BEGIN{in=0}
    /^\s*\[delta\]\s*(#.*)?$/ {in=1; next}
    /^\s*\[/ {in=0}
    in {
      line=$0
      sub(/;.*/,"",line)
      # keep only lines containing "=" (key = value)
      if (line ~ /=/) {
        # remove inline comments starting with "#"
        sub(/#[^"].*$/,"",line)
        gsub(/\r/,"",line)
        print line
      }
    }
  ' "$CONF_FILE"
)

# ----------------------------
# Sanity checks
# ----------------------------
need_cmd borg
need_cmd awk
need_cmd sed
need_cmd grep
need_cmd virsh

[[ -n "$CFG_vm_repo" ]]      || die "vm_repo missing in [delta] of $CONF_FILE"
[[ -d "$CFG_vm_repo" ]]      || die "vm_repo does not exist: $CFG_vm_repo"
[[ -f "$CFG_vm_repo/config" ]] || die "vm_repo is not a borg repo (missing config): $CFG_vm_repo"

[[ -n "$CFG_backup_root" ]]  || warn "backup_root missing; skipping mountpoint check."
if [[ "$CFG_require_mountpoint" == "1" && -n "$CFG_backup_root" ]]; then
  need_cmd findmnt
  if ! findmnt -rno TARGET "$CFG_backup_root" >/dev/null 2>&1; then
    die "require_mountpoint=true but $CFG_backup_root is not a mountpoint."
  fi
fi

# Pass handling
if [[ -n "$CFG_vm_passfile" ]]; then
  [[ -r "$CFG_vm_passfile" ]] || die "vm_passfile not readable: $CFG_vm_passfile"
  export BORG_PASSCOMMAND="cat $CFG_vm_passfile"
  info "Using vm_passfile: $CFG_vm_passfile"
else
  # fallback to interactive
  printf "Enter VM repo passphrase (input hidden): " >/dev/tty
  read -rs PASSPH </dev/tty
  echo >/dev/tty
  [[ -n "${PASSPH:-}" ]] || die "Empty passphrase."
  export BORG_PASSPHRASE="$PASSPH"
  info "Using interactive passphrase."
fi

REPO="$CFG_vm_repo"
info "VM repository: $REPO"

# --------------------------------------
# Archive menu (newest first, one per line)
# --------------------------------------
choose_archive() {
  local repo="$1"
  local -a ARCHIVES=()
  LC_ALL=C readarray -t ARCHIVES < <(borg list "$repo" --format '{archive}{NL}' | sort -r | head -n 50)
  ((${#ARCHIVES[@]})) || die "No archives found in $repo"

  echo "=== Available VM backups (latest 50) ===" >/dev/tty
  for i in "${!ARCHIVES[@]}"; do
    printf "%2d) %s\n" $((i+1)) "${ARCHIVES[i]}" >/dev/tty
  done

  local sel
  while true; do
    printf "Choose an archive [1-%d]: " "${#ARCHIVES[@]}" >/dev/tty
    read -r sel </dev/tty
    [[ "$sel" =~ ^[0-9]+$ ]] && (( sel>=1 && sel<=${#ARCHIVES[@]} )) && { echo "${ARCHIVES[sel-1]}"; return; }
    echo "Invalid selection." >/dev/tty
  done
}

# --------------------------------------
# Archive contents → paths
# --------------------------------------
archive_paths() {
  local repo="$1" arch="$2"
  borg list "$repo"::"$arch" --format '{path}{NL}'
}

# --------------------------------------
# Derive VM names from XMLs/disks
# --------------------------------------
derive_vm_names() {
  awk '
    /^etc\/libvirt\/qemu\/[^/]+\.xml$/ {
      n=$0; sub(/^etc\/libvirt\/qemu\//,"",n); sub(/\.xml$/,"",n); xml[n]=1
    }
    /^var\/lib\/libvirt\/images\/[^/]+\.(qcow2|qcow|img|raw)$/ {
      n=$0; sub(/^var\/lib\/libvirt\/images\//,"",n)
      sub(/\.(qcow2|qcow|img|raw)$/,"",n)
      sub(/-.*/,"",n)
      img[n]=1
    }
    END {
      c=0
      for (n in xml) { print n; c++ }
      if (c==0) for (n in img) print n
    }
  '
}

choose_vm() {
  local names=("$@")
  ((${#names[@]})) || die "No VMs detected."
  echo "=== VMs present in the archive ===" >/dev/tty
  for i in "${!names[@]}"; do
    printf "%2d) %s\n" $((i+1)) "${names[i]}" >/dev/tty
  done
  local sel
  while true; do
    printf "Choose a VM [1-%d]: " "${#names[@]}" >/dev/tty
    read -r sel </dev/tty
    [[ "$sel" =~ ^[0-9]+$ ]] && (( sel>=1 && sel<=${#names[@]} )) && { echo "${names[sel-1]}"; return; }
    echo "Invalid selection." >/dev/tty
  done
}

# --------------------------------------
# Collect all files for a VM
# --------------------------------------
collect_vm_files() {
  local vm="$1"
  awk -v vm="$vm" '
    $0 ~ "^var/lib/libvirt/images/" vm "\\.(qcow2|qcow|img|raw)$" { print; next }
    $0 ~ "^var/lib/libvirt/images/" vm "-[^/]+\\.(qcow2|qcow|img|raw)$" { print; next }
    $0 ~ "^var/lib/libvirt/images/[^/]*" vm "[^/]*\\.(qcow2|qcow|img|raw)$" { print; next }
    $0 == "etc/libvirt/qemu/" vm ".xml" { print; next }
  '
}

main() {
  local ARCHIVE VM_NAME
  ARCHIVE="$(choose_archive "$REPO")"
  info "Selected archive: $ARCHIVE"

  local -a PATHS=()
  LC_ALL=C readarray -t PATHS < <(archive_paths "$REPO" "$ARCHIVE")

  local -a VM_NAMES=()
  LC_ALL=C readarray -t VM_NAMES < <(printf '%s\n' "${PATHS[@]}" | derive_vm_names | sort -u)
  ((${#VM_NAMES[@]})) || die "No VMs found in archive $ARCHIVE."

  VM_NAME="$(choose_vm "${VM_NAMES[@]}")"
  info "Selected VM: $VM_NAME"

  local -a VM_FILES=()
  LC_ALL=C readarray -t VM_FILES < <(printf '%s\n' "${PATHS[@]}" | collect_vm_files "$VM_NAME" | sort -u)
  ((${#VM_FILES[@]})) || die "No files for VM '$VM_NAME' in archive '$ARCHIVE'."

  echo "=== Plan ==="
  echo "Archive: $ARCHIVE"
  echo "VM:      $VM_NAME"
  echo "Files:"
  for f in "${VM_FILES[@]}"; do echo "  /$f"; done
  echo

  local OVERWRITE=0
  for f in "${VM_FILES[@]}"; do
    [[ -e "/$f" ]] && { warn "Target exists: /$f"; OVERWRITE=1; }
  done
  if (( OVERWRITE )); then
    printf "Overwrite existing files? [y/N] " >/dev/tty
    read -r ans </dev/tty
    case "${ans:-}" in y|Y|yes|YES) ;; *) echo "Aborted." >/dev/tty; exit 1 ;; esac
  fi

  mkdir -p /var/lib/libvirt/images
  mkdir -p /etc/libvirt/qemu

  echo "=== Extracting files ==="
  cd /
  borg extract --lock-wait "${CFG_lock_wait}" --progress "$REPO"::"$ARCHIVE" "${VM_FILES[@]}"

  if [[ -e "/etc/libvirt/qemu/${VM_NAME}.xml" ]]; then
    info "Defining VM ${VM_NAME} ..."
    virsh define "/etc/libvirt/qemu/${VM_NAME}.xml" || true
  else
    warn "No XML found for ${VM_NAME}; assuming VM is already defined."
  fi

  info "Starting VM ${VM_NAME} ..."
  if ! virsh start "${VM_NAME}"; then
    warn "Could not start VM ${VM_NAME} automatically. Check with: virsh list --all"
  fi

  echo "=== Done restoring VM '${VM_NAME}' from archive '${ARCHIVE}'. ==="
}

main
