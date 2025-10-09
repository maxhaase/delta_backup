#!/usr/bin/env python3
# ================================================================================
# Project: delta-backup - Backup a server/workstation and VMs it hosts (if any)
# Author: Max Haase – maxhaase@gmail.com
# =================================================================================

import os, sys, time, shutil, socket, shlex, subprocess, configparser, argparse
import xml.etree.ElementTree as ET
from datetime import datetime

# === Constants ===
CONFIG_FILE = "/etc/delta-backup.conf"
CRITICAL_VMS = ["PROD"]  # List of critical VMs to verify are running after backup

# === CLI Arguments ===
# If you run it interactively, you might want to use -p to see the --progress, instead of silent for scheduler use. 
parser = argparse.ArgumentParser(description="Run host + VM backups")
parser.add_argument("-p", "--progress", action="store_true", help="Show per-file copy progress")
args = parser.parse_args()

# === Utilities ===
def die(msg, rc=1):
    print(f"[ERROR] {msg}", file=sys.stderr)
    sys.exit(rc)

def run(cmd, check=True, capture_output=False, env=None):
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
        die(f"Missing config file: {CONFIG_FILE}")
    config.read(CONFIG_FILE)

    delta = config['delta']
    return {
        'backup_root': clean_config_value(delta.get('backup_root')),
        'host_repo': clean_config_value(delta.get('host_repo')),
        'vm_repo': clean_config_value(delta.get('vm_repo')),
        'host_passfile': clean_config_value(delta.get('host_passfile')),
        'vm_passfile': clean_config_value(delta.get('vm_passfile')),
        'host_excludes': [x.strip() for x in clean_config_value(delta.get('host_excludes', '')).split(',') if x.strip()],
        'extra_paths': [x.strip() for x in clean_config_value(delta.get('extra_paths', '')).split(',') if x.strip()],
        'extra_prefix': clean_config_value(delta.get('extra_prefix', 'extra')),
        'lock_file': clean_config_value(delta.get('lock_file', '/var/lock/max-backup.lock')),
        'vm_shutdown_timeout': int(clean_config_value(delta.get('vm_shutdown_timeout', '600'))),
        'vm_startup_grace': int(clean_config_value(delta.get('vm_startup_grace', '5'))),
        'engine_compression': clean_config_value(delta.get('engine_compression', 'zstd,6')),
        'engine_filter': clean_config_value(delta.get('engine_filter', 'AME')),
        'engine_files_cache': clean_config_value(delta.get('engine_files_cache', 'ctime,size,inode')),
        'engine_one_file_system': clean_config_value(delta.get('engine_one_file_system', 'true')).lower() in ('1','yes','true','on'),
    }

CONFIG = load_config()

# === Paths ===
HOST_REPO = CONFIG['host_repo']
VM_REPO = CONFIG['vm_repo']
HOST_PASSFILE = CONFIG['host_passfile']
VM_PASSFILE = CONFIG['vm_passfile']
LOCK_FILE = CONFIG['lock_file']
STAGING_DIR = os.path.join(VM_REPO, ".staging")
BACKUP_MOUNT = os.path.dirname(CONFIG['backup_root'].rstrip('/'))

# === Locking ===
def acquire_lock():
    if os.path.exists(LOCK_FILE):
        die(f"Lock file exists: {LOCK_FILE}")
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))

def release_lock():
    try:
        os.remove(LOCK_FILE)
    except FileNotFoundError:
        pass

# === Borg ===
def borg_env(passfile):
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
        "--filter", CONFIG['engine_filter'],
        "--compression", CONFIG['engine_compression'],
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

# === VM & Disk Helpers ===
def list_domains():
    res = run(["virsh", "list", "--all", "--name"], capture_output=True, check=False)
    return [line.strip() for line in res.stdout.splitlines() if line.strip()]

def get_disks_from_xml(vm_name):
    res = run(["virsh", "dumpxml", vm_name], capture_output=True, check=False)
    disks = []
    try:
        root = ET.fromstring(res.stdout)
        for disk in root.findall(".//devices/disk"):
            source = disk.find("source")
            if source is not None:
                for key in ("file", "dev", "name"):
                    val = source.get(key)
                    if val and os.path.exists(val):
                        disks.append(val)
                        break
    except ET.ParseError:
        pass
    return disks

def is_on_backup_volume(path):
    try:
        return os.path.realpath(path).startswith(BACKUP_MOUNT)
    except Exception:
        return False

def copy_with_progress(src, dest):
    total = os.path.getsize(src)
    copied = 0
    block = 1024 * 1024  # 1 MB
    with open(src, 'rb') as fsrc, open(dest, 'wb') as fdst:
        while True:
            buf = fsrc.read(block)
            if not buf:
                break
            fdst.write(buf)
            copied += len(buf)
            percent = copied * 100 // total
            print(f"\r  Copied: {copied // (1024*1024)} MB of {total // (1024*1024)} MB ({percent}%)", end='', flush=True)
        print()

def backup_vm_disks(vm_name, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    disks = get_disks_from_xml(vm_name)
    files = []
    seen = set()
    for src in disks:
        if src in seen or is_on_backup_volume(src):
            continue
        seen.add(src)
        dest = os.path.join(out_dir, os.path.basename(src))
        print(f"[INFO] Copying disk {src} to {dest}")
        if args.progress:
            copy_with_progress(src, dest)
        else:
            run(["cp", "--sparse=always", src, dest])
        files.append(dest)
    xml_path = os.path.join(out_dir, f"{vm_name}.xml")
    with open(xml_path, "w") as f:
        subprocess.run(["virsh", "dumpxml", vm_name], stdout=f)
    files.append(xml_path)
    return files

def shutdown_vm(vm):
    run(["virsh", "shutdown", vm])
    for _ in range(CONFIG['vm_shutdown_timeout']):
        state = run(["virsh", "domstate", vm], capture_output=True, check=False).stdout.strip().lower()
        if state == "shut off":
            return
        time.sleep(1)
    die(f"VM {vm} did not shut down in time")

def start_vm(vm):
    run(["virsh", "start", vm])
    time.sleep(CONFIG['vm_startup_grace'])

def is_vm_running(vm):
    state = run(["virsh", "domstate", vm], capture_output=True, check=False).stdout.strip().lower()
    return state == "running"

def ensure_critical_vms_running():
    for vm in CRITICAL_VMS:
        if not is_vm_running(vm):
            print(f"[ALERT] Critical VM {vm} is not running, attempting restart…")
            start_vm(vm)
            if not is_vm_running(vm):
                print(f"[FAIL] VM {vm} could not be restarted.")

# === MAIN ===
def main():
    acquire_lock()
    try:
        hostname = socket.gethostname()
        print("=== HOST BACKUP ===")
        borg_create(HOST_REPO, HOST_PASSFILE, ["/"], CONFIG['host_excludes'], prefix=hostname, comment="Host backup")

        print("=== EXTRA PATHS ===")
        for i, path in enumerate(CONFIG['extra_paths']):
            if os.path.exists(path):
                borg_create(HOST_REPO, HOST_PASSFILE, [path], prefix=f"{hostname}-{CONFIG['extra_prefix']}-{i}", comment=f"Extra: {path}")
            else:
                print(f"[SKIP] Path not found: {path}")

        print("=== VM BACKUPS ===")
        domains = list_domains()
        for vm in domains:
            print(f"\n--- VM: {vm} ---")
            vm_staging = os.path.join(STAGING_DIR, vm)
            if os.path.exists(vm_staging):
                shutil.rmtree(vm_staging)
            try:
                shutdown_vm(vm)
                files = backup_vm_disks(vm, vm_staging)
            finally:
                start_vm(vm)
            if files:
                borg_create(VM_REPO, VM_PASSFILE, [vm_staging], prefix=f"{hostname}-{vm}", comment=f"VM cold backup: {vm}")
    finally:
        release_lock()
        ensure_critical_vms_running()

if __name__ == "__main__":
    main()
