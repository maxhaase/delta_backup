#!/usr/bin/env python3
import os, subprocess, shlex, time, socket, sys, shutil, configparser
import xml.etree.ElementTree as ET
from datetime import datetime
# ==============================================================
# Project: delta-backup - Backup a server/workstation and VMs it hosts (if any)
# Author: Max Haase – maxhaase@gmail.com
# ===============================================================
# === CONFIGURATION FROM FILE ===
CONFIG_FILE = "/etc/delta-backup.conf"

# === HELPERS ===
def die(msg, rc=1):
    print(f"[ERROR] {msg}", file=sys.stderr)
    sys.exit(rc)

def clean_config_value(value):
    """Remove inline comments and strip whitespace from config values"""
    if not value:
        return value
    # Split on '#' to remove inline comments, then strip whitespace
    return value.split('#')[0].strip()

def load_config():
    """Load configuration from config file"""
    config = configparser.ConfigParser()
    if not os.path.exists(CONFIG_FILE):
        die(f"Configuration file not found: {CONFIG_FILE}")
    
    config.read(CONFIG_FILE)
    
    if 'delta' not in config:
        die("No [delta] section found in configuration file")
    
    delta = config['delta']
    
    # Required settings
    backup_root = clean_config_value(delta.get('backup_root'))
    if not backup_root:
        die("backup_root not set in configuration")
    
    host_repo = clean_config_value(delta.get('host_repo', os.path.join(backup_root, 'host-backup')))
    vm_repo = clean_config_value(delta.get('vm_repo', os.path.join(backup_root, 'vm-backup')))
    host_passfile = clean_config_value(delta.get('host_passfile'))
    vm_passfile = clean_config_value(delta.get('vm_passfile'))
    
    if not all([host_repo, vm_repo, host_passfile, vm_passfile]):
        die("Missing required configuration: host_repo, vm_repo, host_passfile, or vm_passfile")
    
    # Parse excludes
    host_excludes_str = clean_config_value(delta.get('host_excludes', ''))
    host_excludes = [exclude.strip() for exclude in host_excludes_str.split(',') if exclude.strip()]
    
    # Parse extra paths
    extra_paths_str = clean_config_value(delta.get('extra_paths', ''))
    extra_paths = [path.strip() for path in extra_paths_str.split(',') if path.strip()]
    
    # Other settings with defaults
    lock_file = clean_config_value(delta.get('lock_file', '/var/lock/max-backup.lock'))
    
    # Handle integer values with comments
    vm_shutdown_timeout_str = clean_config_value(delta.get('vm_shutdown_timeout', '600'))
    vm_shutdown_timeout = int(vm_shutdown_timeout_str) if vm_shutdown_timeout_str else 600
    
    vm_startup_grace_str = clean_config_value(delta.get('vm_startup_grace', '5'))
    vm_startup_grace = int(vm_startup_grace_str) if vm_startup_grace_str else 5
    
    engine_compression = clean_config_value(delta.get('engine_compression', 'zstd,6'))
    engine_filter = clean_config_value(delta.get('engine_filter', 'AME'))
    engine_files_cache = clean_config_value(delta.get('engine_files_cache', 'ctime,size,inode'))
    
    return {
        'backup_root': backup_root,
        'host_repo': host_repo,
        'vm_repo': vm_repo,
        'host_passfile': host_passfile,
        'vm_passfile': vm_passfile,
        'host_excludes': host_excludes,
        'extra_paths': extra_paths,
        'extra_prefix': clean_config_value(delta.get('extra_prefix', 'extra')),
        'lock_file': lock_file,
        'vm_shutdown_timeout': vm_shutdown_timeout,
        'vm_startup_grace': vm_startup_grace,
        'engine_compression': engine_compression,
        'engine_filter': engine_filter,
        'engine_files_cache': engine_files_cache,
        'enable_prune': clean_config_value(delta.get('enable_prune', 'false')).lower() in ('true', 'yes', 'on', '1'),
        'enable_compact': clean_config_value(delta.get('enable_compact', 'true')).lower() in ('true', 'yes', 'on', '1'),
    }

# Load configuration
try:
    CONFIG = load_config()
except Exception as e:
    die(f"Failed to load configuration: {e}")

# === DERIVED CONFIGURATION ===
HOST_REPO = CONFIG['host_repo']
VM_REPO = CONFIG['vm_repo']
HOST_PASSFILE = CONFIG['host_passfile']
VM_PASSFILE = CONFIG['vm_passfile']
LOCK_FILE = CONFIG['lock_file']
BACKUP_MOUNT = os.path.dirname(CONFIG['backup_root'].rstrip('/'))  # Parent of backup_root
STAGING_DIR = os.path.join(VM_REPO, ".staging")

# Critical VMs that MUST be running after backup
CRITICAL_VMS = ["XSOL"]  # Add other critical VMs to this list

# Paths to include in host backup
INCLUDE_PATHS = [
    "/bin", "/boot", "/etc", "/home", "/lib", "/lib64",
    "/opt", "/root", "/sbin", "/srv", "/usr", "/var"
]

HOST_EXCLUDES = CONFIG['host_excludes']
EXTRA_PATHS = CONFIG['extra_paths']

def run(cmd, check=True, capture_output=False, env=None):
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)
    print(f"[CMD] {' '.join(shlex.quote(c) for c in cmd)}")
    return subprocess.run(cmd, check=check, capture_output=capture_output, text=True, env=env)

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
        "--comment", comment or f"Backup on {hostname}",
    ]
    
    if CONFIG.get('engine_one_file_system', True):
        cmd.append("--one-file-system")
    
    if excludes:
        for ex in excludes:
            cmd.extend(["--exclude", ex])
    cmd.append(archive_loc)
    cmd.extend(sources)
    return run(cmd, check=False, env=borg_env(passfile)).returncode

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

def list_domains():
    res = run(["virsh", "list", "--all", "--name"], capture_output=True, check=False)
    return [line.strip() for line in res.stdout.splitlines() if line.strip()]

def shutdown_vm(vm):
    run(["virsh", "shutdown", vm])
    print(f"[INFO] Waiting for VM {vm} to shut down (timeout: {CONFIG['vm_shutdown_timeout']}s)…")
    for _ in range(CONFIG['vm_shutdown_timeout']):
        state = run(["virsh", "domstate", vm], capture_output=True, check=False).stdout.strip().lower()
        if state == "shut off":
            print(f"[INFO] VM {vm} is shut off.")
            return
        time.sleep(1)
    die(f"VM {vm} did not shut down in {CONFIG['vm_shutdown_timeout']} seconds")

def start_vm(vm):
    run(["virsh", "start", vm])
    print(f"[INFO] VM {vm} started.")
    time.sleep(CONFIG['vm_startup_grace'])  # Grace period after starting

def dump_vm_xml(vm, out_path):
    with open(out_path, "w") as f:
        subprocess.run(["virsh", "dumpxml", vm], stdout=f, check=True)

def get_disks_from_xml(vm_name):
    res = run(["virsh", "dumpxml", vm_name], capture_output=True, check=False)
    disks = []
    try:
        root = ET.fromstring(res.stdout)
        for disk in root.findall(".//devices/disk"):
            source = disk.find("source")
            if source is not None:
                for key in ("file", "dev", "name"):
                    file_attr = source.get(key)
                    if file_attr and os.path.exists(file_attr):
                        disks.append(file_attr)
                        break
    except ET.ParseError as e:
        print(f"[ERROR] XML parse failed for VM {vm_name}: {e}")
    return disks

def is_on_backup_volume(path):
    try:
        real = os.path.realpath(path)
        return real.startswith(BACKUP_MOUNT)
    except Exception:
        return False

def backup_vm_disks(vm_name, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    disks = get_disks_from_xml(vm_name)
    if not disks:
        print(f"[WARN] No valid disks found for VM {vm_name}")
        return []

    files = []
    seen = set()
    for source in disks:
        if source in seen:
            continue
        seen.add(source)

        if is_on_backup_volume(source):
            print(f"[SKIP] Skipping {source} (on backup volume)")
            continue

        dest = os.path.join(out_dir, os.path.basename(source))
        print(f"[INFO] Copying disk {source} to {dest}")
        run(["cp", "--sparse=always", source, dest])
        files.append(dest)

    xml_path = os.path.join(out_dir, f"{vm_name}.xml")
    dump_vm_xml(vm_name, xml_path)
    files.append(xml_path)
    return files

# === NEW VM STATUS FUNCTIONS ===
def get_vm_state(vm_name):
    """Get the current state of a VM"""
    try:
        result = run(["virsh", "domstate", vm_name], capture_output=True, check=False)
        return result.stdout.strip().lower()
    except Exception as e:
        print(f"[WARN] Could not get state for VM {vm_name}: {e}")
        return "unknown"

def is_vm_running(vm_name):
    """Check if VM is in a running state"""
    state = get_vm_state(vm_name)
    return state in ["running", "idle", "paused"]

def wait_for_vm_start(vm_name, timeout=120):
    """Wait for VM to reach running state with timeout"""
    print(f"[INFO] Waiting for VM {vm_name} to start (timeout: {timeout}s)...")
    for i in range(timeout):
        state = get_vm_state(vm_name)
        if state in ["running", "idle"]:
            print(f"[SUCCESS] VM {vm_name} is now running")
            return True
        elif state == "paused":
            print(f"[INFO] VM {vm_name} is paused, resuming...")
            run(["virsh", "resume", vm_name], check=False)
        elif state == "shut off":
            print(f"[INFO] VM {vm_name} is shut off, starting...")
            run(["virsh", "start", vm_name], check=False)
        elif state == "crashed":
            print(f"[WARN] VM {vm_name} is crashed, attempting to start...")
            run(["virsh", "start", vm_name], check=False)
        
        if i % 10 == 0:  # Print status every 10 seconds
            print(f"[INFO] VM {vm_name} current state: {state} ({i}s elapsed)")
        time.sleep(1)
    
    print(f"[ERROR] VM {vm_name} failed to start within {timeout} seconds")
    return False

def ensure_critical_vms_running():
    """Ensure all critical VMs are running, attempt recovery if not"""
    print("\n=== VERIFYING CRITICAL VM STATUS ===")
    all_running = True
    
    for vm in CRITICAL_VMS:
        print(f"\n--- Checking critical VM: {vm} ---")
        if is_vm_running(vm):
            print(f"[SUCCESS] Critical VM {vm} is running")
        else:
            print(f"[ALERT] Critical VM {vm} is NOT running! Attempting recovery...")
            current_state = get_vm_state(vm)
            print(f"[INFO] Current state of {vm}: {current_state}")
            
            # Attempt to start the VM
            try:
                if current_state in ["shut off", "crashed", "unknown"]:
                    print(f"[ACTION] Starting VM {vm}...")
                    run(["virsh", "start", vm], check=False)
                elif current_state == "paused":
                    print(f"[ACTION] Resuming VM {vm}...")
                    run(["virsh", "resume", vm], check=False)
                
                # Wait for it to become running
                if wait_for_vm_start(vm):
                    print(f"[RECOVERY] Successfully started VM {vm}")
                else:
                    print(f"[ERROR] Failed to start VM {vm}")
                    all_running = False
                    
            except Exception as e:
                print(f"[ERROR] Failed to recover VM {vm}: {e}")
                all_running = False
    
    # Additional check: verify all VMs that were backed up are running
    print("\n--- Final status of all backed up VMs ---")
    try:
        domains = list_domains()
        for vm in domains:
            state = get_vm_state(vm)
            status = "RUNNING" if is_vm_running(vm) else "NOT RUNNING"
            print(f"  {vm}: {status} ({state})")
    except Exception as e:
        print(f"[WARN] Could not verify all VMs: {e}")
    
    return all_running

# === MAIN ===
def main():
    if os.geteuid() != 0:
        die("Must be run as root")
    if not os.path.isdir(HOST_REPO):
        die(f"Missing host repo: {HOST_REPO}")
    if not os.path.isdir(VM_REPO):
        die(f"Missing VM repo: {VM_REPO}")
    if not os.path.exists(HOST_PASSFILE) or not os.path.exists(VM_PASSFILE):
        die("Missing passfile(s)")

    start_time = time.time()
    backup_successful = True

    try:
        acquire_lock()
        hostname = socket.gethostname()

        print("\n=== HOST BACKUP ===")
        rc = borg_create(HOST_REPO, HOST_PASSFILE, INCLUDE_PATHS, HOST_EXCLUDES, prefix=hostname, comment=f"Host filesystem backup ({hostname})")
        if rc != 0:
            print("[WARN] Host backup completed with warnings/errors")

        # Backup extra paths if configured
        if EXTRA_PATHS:
            print(f"\n=== EXTRA PATHS BACKUP ===")
            for i, path in enumerate(EXTRA_PATHS):
                if os.path.exists(path):
                    print(f"\n--- Backing up: {path} ---")
                    prefix = f"{hostname}-{CONFIG['extra_prefix']}-{i}"
                    comment = f"Extra path backup: {path} on {hostname}"
                    rc = borg_create(HOST_REPO, HOST_PASSFILE, [path], excludes=None, prefix=prefix, comment=comment)
                    if rc != 0:
                        print(f"[WARN] Extra path backup for {path} completed with warnings/errors")
                else:
                    print(f"[WARN] Extra path not found: {path}")

        print("\n=== VM BACKUPS ===")
        domains = list_domains()
        if not domains:
            print("[INFO] No VMs found.")

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
                rc = borg_create(VM_REPO, VM_PASSFILE, [vm_staging], excludes=None, prefix=f"{hostname}-{vm}", comment=f"VM cold backup (shutdown) for {vm} on {hostname}")
                if rc != 0:
                    print(f"[WARN] VM backup for {vm} completed with warnings/errors")
            else:
                print(f"[WARN] No files were backed up for VM {vm}")

    except Exception as e:
        backup_successful = False
        print(f"[ERROR] Backup process failed: {e}")
    finally:
        release_lock()
        
        # CRITICAL: Ensure all VMs are running after backup
        print("\n" + "="*60)
        print("FINAL VM STATUS VERIFICATION")
        print("="*60)
        
        critical_vms_ok = ensure_critical_vms_running()
        
        end_time = time.time()
        duration = int(end_time - start_time)
        hours, minutes = divmod(duration // 60, 60)
        
        print(f"\n=== BACKUP SUMMARY ===")
        print(f"Duration: {hours} hour(s) and {minutes} minute(s)")
        print(f"Backup process: {'COMPLETED' if backup_successful else 'FAILED'}")
        print(f"Critical VMs status: {'ALL RUNNING' if critical_vms_ok else 'SOME CRITICAL VMs OFFLINE!'}")
        
        if not critical_vms_ok:
            print("\n🚨 ALERT: Some critical VMs are not running!")
            print("   Immediate attention required!")
            # Exit with error code if critical VMs are down
            sys.exit(2)

if __name__ == "__main__":
    main()
