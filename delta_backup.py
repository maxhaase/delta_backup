#!/usr/bin/env python3
# ==============================================================
# Project: delta-backup - Safe cold backup of host and VMs
# Author: Max Haase – maxhaase@gmail.com
# License: MIT
# ==============================================================

import os, subprocess, shlex, time, socket, sys, shutil, configparser
from datetime import datetime, timezone
import xml.etree.ElementTree as ET

CONFIG_FILE = "/etc/delta-backup.conf"

# === HELPERS ===
def die(msg, rc=1):
    print(f"[ERROR] {msg}", file=sys.stderr)
    sys.exit(rc)

def format_size(size_bytes):
    """Convert bytes to human readable format"""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} PB"

def format_duration(seconds):
    """Convert seconds to human readable duration"""
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        minutes = seconds // 60
        seconds = seconds % 60
        return f"{minutes:.0f}m {seconds:.0f}s"
    else:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours:.0f}h {minutes:.0f}m"

def run(cmd, check=True, capture_output=False, env=None, show_progress=False):
    """Run a shell command with logging."""
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)
    print(f"[CMD] {' '.join(shlex.quote(c) for c in cmd)}")
    
    if show_progress:
        # For Borg commands, let it output directly to terminal for real-time progress
        # This is crucial for Borg's progress display to work properly
        process = subprocess.Popen(cmd, env=env)
        rc = process.wait()
        
        if check and rc != 0:
            die(f"Command failed with exit code {rc}")
        return rc
    else:
        return subprocess.run(cmd, check=check, capture_output=capture_output, text=True, env=env).returncode

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
    host_passfile = clean_config_value(delta.get('host_passfile'))
    host_excludes = [x.strip() for x in clean_config_value(delta.get('host_excludes', '')).split(',') if x.strip()]
    extra_paths = [x.strip() for x in clean_config_value(delta.get('extra_paths', '')).split(',') if x.strip()]
    
    # Load include_paths from config, fall back to default if not specified
    include_paths_config = clean_config_value(delta.get('include_paths', ''))
    if include_paths_config:
        include_paths = [x.strip() for x in include_paths_config.split(',') if x.strip()]
    else:
        # Default include paths if not specified in config
        include_paths = ["/bin", "/boot", "/etc", "/home", "/lib", "/lib64", "/opt", "/root", "/sbin", "/srv", "/usr", "/var"]

    return {
        'backup_root': backup_root,
        'host_repo': host_repo,
        'host_passfile': host_passfile,
        'host_excludes': host_excludes,
        'include_paths': include_paths,
        'extra_paths': extra_paths,
        'extra_prefix': clean_config_value(delta.get('extra_prefix', 'extra')),
        'lock_file': clean_config_value(delta.get('lock_file', '/var/lock/max-backup.lock')),
        'lock_wait': clean_config_value(delta.get('lock_wait', '120')),
        'engine_compression': clean_config_value(delta.get('engine_compression', 'zstd,6')),
        'engine_filter': clean_config_value(delta.get('engine_filter', 'AME')),
        'engine_files_cache': clean_config_value(delta.get('engine_files_cache', 'ctime,size,inode')),
        'engine_one_file_system': True
    }

CONFIG = load_config()
HOST_REPO = CONFIG['host_repo']
HOST_PASSFILE = CONFIG['host_passfile']
LOCK_FILE = CONFIG['lock_file']
INCLUDE_PATHS = CONFIG['include_paths']  # Now loaded from config

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
    env["BORG_LOCK_WAIT"] = CONFIG['lock_wait']  # Use configured lock wait time
    return env

def borg_create(repo, passfile, sources, excludes=None, prefix=None, comment=None):
    hostname = socket.gethostname()
    # Fixed: Use timezone-aware datetime to avoid deprecation warning
    archive = f"{(prefix or hostname)}-{datetime.now(timezone.utc).strftime('%Y-%m-%d_%H-%M')}"
    archive_loc = f"{repo}::{archive}"
    cmd = [
        "borg", "create", "--verbose", "--stats", "--show-rc", "--list", "--progress",
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
    
    print(f"[INFO] Starting backup: {archive}")
    print(f"[INFO] Sources: {sources}")
    if excludes:
        print(f"[INFO] Excludes: {excludes}")
    print("[PROGRESS] Starting backup with detailed progress...\n")
    
    return run(cmd, check=False, env=borg_env(passfile), show_progress=True)

def check_and_fix_borg_lock():
    """Check for Borg lock and break it if stale"""
    lock_path = os.path.join(HOST_REPO, "lock")
    lock_exempt_path = os.path.join(HOST_REPO, "lock.exclusive")
    
    # Check if lock files exist
    has_lock = os.path.exists(lock_path) or os.path.exists(lock_exempt_path)
    
    if has_lock:
        print(f"[WARN] Borg lock file(s) detected in {HOST_REPO}")
        print("[INFO] Checking if lock is stale...")
        
        # Try to list repositories to see if we can access it
        env = borg_env(HOST_PASSFILE)
        test_cmd = ["borg", "list", "--short", HOST_REPO]
        result = subprocess.run(test_cmd, env=env, capture_output=True, text=True)
        
        if result.returncode != 0 and "lock" in result.stderr:
            print("[WARN] Repository appears to be locked. Breaking stale lock...")
            
            # Break the lock using borg break-lock
            break_cmd = ["borg", "break-lock", HOST_REPO]
            break_result = subprocess.run(break_cmd, env=env, capture_output=True, text=True)
            
            if break_result.returncode == 0:
                print("[INFO] Successfully broke stale Borg lock")
            else:
                print(f"[ERROR] Failed to break lock: {break_result.stderr}")
                print("[INFO] You may need to manually break the lock with: borg break-lock /STORE/BACKUP/host-backup")
        else:
            print("[INFO] Repository is accessible, lock appears to be valid")
    else:
        print("[INFO] No Borg lock files detected")

# === MAIN LOGIC ===
def main():
    if os.geteuid() != 0:
        die("Must be run as root")
    
    # Check if Borg repository exists and is accessible
    if not os.path.exists(HOST_REPO):
        die(f"Borg repository not found: {HOST_REPO}")
    
    # Check for stale Borg locks before starting
    check_and_fix_borg_lock()
    
    acquire_lock()
    start_time = time.time()
    hostname = socket.gethostname()

    try:
        print("\n=== HOST BACKUP (including VM images) ===")
        print("[INFO] VM disk images in /var/lib/libvirt/images are now included in host backup")
        
        rc = borg_create(HOST_REPO, HOST_PASSFILE, INCLUDE_PATHS, excludes=CONFIG['host_excludes'], prefix=hostname)
        if rc != 0:
            print("[WARN] Host backup completed with warnings")

        print("\n=== EXTRA PATHS BACKUP ===")
        for i, path in enumerate(CONFIG['extra_paths']):
            if os.path.exists(path):
                print(f"[INFO] Backing up extra path: {path}")
                prefix = f"{hostname}-{CONFIG['extra_prefix']}-{i}"
                borg_create(HOST_REPO, HOST_PASSFILE, [path], prefix=prefix, comment=f"Extra path {path}")
            else:
                print(f"[SKIP] Extra path not found: {path}")

    finally:
        release_lock()
        dur = int(time.time() - start_time)
        h, m = divmod(dur // 60, 60)
        print(f"\n=== DONE: Backup completed in {h} hour(s) {m} min(s) ===")

if __name__ == "__main__":
    main()
