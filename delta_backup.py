#!/usr/bin/env python3
# ==============================================================
# Project: delta-backup - Safe cold backup of host and VMs
# Customized for use with VMM (libvirt)
# Author: Max Haase – maxhaase@gmail.com
# License: MIT
# ==============================================================

import os, subprocess, shlex, time, socket, sys, shutil, configparser
from datetime import datetime
import xml.etree.ElementTree as ET

CONFIG_FILE = "/etc/delta-backup.conf"

# === HELPERS ===
def die(msg, rc=1):
    print(f"[ERROR] {msg}", file=sys.stderr)
    sys.exit(rc)

def run(cmd, check=True, capture_output=False, env=None):
    """Run a shell command with logging."""
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)
    print(f"[CMD] {' '.join(shlex.quote(c) for c in cmd)}")
    return subprocess.run(cmd, check=check, capture_output=capture_output, text=True, env=env)

def clean_config_value(value):
    if not value:
        return value
    return value.split('#')[0].strip()

def load_config():
    config = configparser.ConfigParser()
    if not os.path.exists(CONFIG_FILE):
        die(f"Configuration file not found: {CONFIG_FILE}")
    config.read(CONFIG_FILE)
    if 'delta' not in config:
        die("No [delta] section found in config")

    delta = config['delta']
    backup_root = clean_config_value(delta.get('backup_root'))
    host_repo = clean_config_value(delta.get('host_repo'))
    vm_repo = clean_config_value(delta.get('vm_repo'))
    host_passfile = clean_config_value(delta.get('host_passfile'))
    vm_passfile = clean_config_value(delta.get('vm_passfile'))
    host_excludes = [x.strip() for x in clean_config_value(delta.get('host_excludes', '')).split(',') if x.strip()]
    extra_paths = [x.strip() for x in clean_config_value(delta.get('extra_paths', '')).split(',') if x.strip()]

    # Always exclude VM disk directory to prevent duplication
    if "/var/lib/libvirt/images" not in host_excludes:
        host_excludes.append("/var/lib/libvirt/images")
    host_excludes.append("*.qcow2")

    return {
        'backup_root': backup_root,
        'host_repo': host_repo,
        'vm_repo': vm_repo,
        'host_passfile': host_passfile,
        'vm_passfile': vm_passfile,
        'host_excludes': host_excludes,
        'extra_paths': extra_paths,
        'extra_prefix': clean_config_value(delta.get('extra_prefix', 'extra')),
        'lock_file': clean_config_value(delta.get('lock_file', '/var/lock/max-backup.lock')),
        'vm_shutdown_timeout': int(clean_config_value(delta.get('vm_shutdown_timeout', '600'))),
        'vm_startup_grace': int(clean_config_value(delta.get('vm_startup_grace', '5'))),
        'engine_compression': clean_config_value(delta.get('engine_compression', 'zstd,6')),
        'engine_filter': clean_config_value(delta.get('engine_filter', 'AME')),
        'engine_files_cache': clean_config_value(delta.get('engine_files_cache', 'ctime,size,inode')),
        'engine_one_file_system': True
    }

CONFIG = load_config()
HOST_REPO = CONFIG['host_repo']
VM_REPO = CONFIG['vm_repo']
HOST_PASSFILE = CONFIG['host_passfile']
VM_PASSFILE = CONFIG['vm_passfile']
LOCK_FILE = CONFIG['lock_file']
STAGING_DIR = os.path.join(VM_REPO, ".staging")
CRITICAL_VMS = ["PROD"]
INCLUDE_PATHS = ["/bin", "/boot", "/etc", "/home", "/lib", "/lib64", "/opt", "/root", "/sbin", "/srv", "/usr", "/var"]

# === LOCK HANDLING ===
def acquire_lock():
    if os.path.exists(LOCK_FILE):
        print(f"[WARN] Stale lock detected, removing old lock file: {LOCK_FILE}")
        os.remove(LOCK_FILE)
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))

def release_lock():
    try:
        os.remove(LOCK_FILE)
    except FileNotFoundError:
        pass

# === BORG WRAPPER ===
def borg_env(passfile):
    if not os.path.isfile(passfile):
        die(f"Cannot read passfile: {passfile}")
    env = os.environ.copy()
    env["BORG_PASSCOMMAND"] = f"cat {passfile}"
    env["BORG_FILES_CACHE"] = CONFIG['engine_files_cache']
    env["BORG_LOCK_WAIT"] = "60"
    return env

def borg_create(repo, passfile, sources, excludes=None, prefix=None, comment=None):
    hostname = socket.gethostname()
    archive = f"{(prefix or hostname)}-{datetime.utcnow().strftime('%Y-%m-%d_%H-%M')}"
    archive_loc = f"{repo}::{archive}"
    cmd = [
        "borg", "create", "--verbose", "--stats", "--show-rc", "--list",
        "--filter", CONFIG['engine_filter'], "--compression", CONFIG['engine_compression'],
        "--comment", comment or f"Backup on {hostname}"
    ]
    if CONFIG['engine_one_file_system']:
        cmd.append("--one-file-system")
    if excludes:
        for ex in excludes:
            cmd.extend(["--exclude", ex])
    cmd.append(archive_loc)
    cmd.extend(sources)
    return run(cmd, check=False, env=borg_env(passfile)).returncode

# === VM BACKUP HANDLING ===
def shutdown_vm(vm):
    run(["virsh", "shutdown", vm])
    print(f"[INFO] Waiting for VM {vm} to shut down…")
    for _ in range(CONFIG['vm_shutdown_timeout']):
        state = subprocess.run(["virsh", "domstate", vm], capture_output=True, text=True).stdout.strip().lower()
        if state == "shut off":
            print(f"[INFO] VM {vm} is shut off.")
            return
        time.sleep(1)
    die(f"VM {vm} did not shut down in time")

def start_vm(vm):
    run(["virsh", "start", vm])
    print(f"[INFO] VM {vm} started.")
    time.sleep(CONFIG['vm_startup_grace'])

def ensure_vm_running(vm):
    state = subprocess.run(["virsh", "domstate", vm], capture_output=True, text=True).stdout.strip().lower()
    if state not in ["running", "idle"]:
        print(f"[WARN] VM {vm} is not running, attempting restart…")
        start_vm(vm)

def copy_with_progress(src, dst):
    """Copy file with pv or rsync progress."""
    try:
        if shutil.which("pv"):
            size_bytes = os.path.getsize(src)
            size_mb = size_bytes // (1024 * 1024)
            print(f"[INFO] Copying with progress: {src} -> {dst} ({size_mb} MB)")
            subprocess.run(f"pv '{src}' > '{dst}'", shell=True, check=True)
        elif shutil.which("rsync"):
            print(f"[INFO] Copying with rsync progress: {src} -> {dst}")
            run(["rsync", "-ah", "--info=progress2", src, dst])
        else:
            print("[INFO] Copying without progress (no pv or rsync available)")
            run(["cp", "--sparse=always", src, dst])
    except Exception as e:
        print(f"[ERROR] Copy failed: {e}")
        die("Copying VM image failed")

def backup_vm_disks(vm, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    disk = f"/var/lib/libvirt/images/{vm}.qcow2"
    if not os.path.exists(disk):
        print(f"[ERROR] Disk not found for VM {vm}: {disk}")
        return []
    dest = os.path.join(out_dir, os.path.basename(disk))
    print(f"[INFO] Copying {disk} to {dest}")
    copy_with_progress(disk, dest)
    xml = os.path.join(out_dir, f"{vm}.xml")
    with open(xml, "w") as f:
        subprocess.run(["virsh", "dumpxml", vm], stdout=f, check=True)
    return [dest, xml]

# === MAIN LOGIC ===
def main():
    if os.geteuid() != 0:
        die("Must be run as root")
    acquire_lock()
    start_time = time.time()
    hostname = socket.gethostname()

    try:
        print("\n=== HOST BACKUP ===")
        rc = borg_create(HOST_REPO, HOST_PASSFILE, INCLUDE_PATHS, excludes=CONFIG['host_excludes'], prefix=hostname)
        if rc != 0:
            print("[WARN] Host backup completed with warnings")

        print("\n=== EXTRA PATHS BACKUP ===")
        for i, path in enumerate(CONFIG['extra_paths']):
            if os.path.exists(path):
                prefix = f"{hostname}-{CONFIG['extra_prefix']}-{i}"
                borg_create(HOST_REPO, HOST_PASSFILE, [path], prefix=prefix, comment=f"Extra path {path}")
            else:
                print(f"[SKIP] Extra path not found: {path}")

        print("\n=== VM BACKUPS ===")
        for vm in CRITICAL_VMS:
            print(f"\n--- VM: {vm} ---")
            staging = os.path.join(STAGING_DIR, vm)
            if os.path.exists(staging):
                shutil.rmtree(staging)
            try:
                shutdown_vm(vm)
                files = backup_vm_disks(vm, staging)
            finally:
                start_vm(vm)
            if files:
                borg_create(VM_REPO, VM_PASSFILE, [staging], prefix=f"{hostname}-{vm}", comment=f"Cold backup for {vm}")
            else:
                print(f"[WARN] No VM disks found for {vm}")

        print("\n=== FINAL CHECK ===")
        for vm in CRITICAL_VMS:
            ensure_vm_running(vm)

    finally:
        release_lock()
        dur = int(time.time() - start_time)
        h, m = divmod(dur // 60, 60)
        print(f"\n=== DONE: Backup completed in {h} hour(s) {m} min(s) ===")

if __name__ == "__main__":
    main()


