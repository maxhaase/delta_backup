#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================
# Project: delta-backup orchestrator
# Author: Max Haase – maxhaase@gmail.com
# ===============================================

import os
import shlex
import subprocess
import time
import socket
import sys
import configparser

# ===== CONFIG LOADING (ONLY from /etc/delta-backup.conf) =====
# You may change the path via DELTA_CONFIG for testing, but NO other env keys are read.
CONF_PATH = os.environ.get("DELTA_CONFIG", "/etc/delta-backup.conf")
cfg = configparser.ConfigParser()
if not os.path.isfile(CONF_PATH):
    print(f"[ERROR] Config file not found: {CONF_PATH}", file=sys.stderr)
    sys.exit(2)
cfg.read(CONF_PATH)

# Returns a required string from the [delta] section, or exits with an error.
def req(opt: str) -> str:
    """Fetch a REQUIRED value from [delta]; exit with error if missing or empty."""
    if not cfg.has_option("delta", opt):
        print(f"[ERROR] Missing required config option: {opt}", file=sys.stderr)
        sys.exit(2)
    val = cfg.get("delta", opt).strip()
    if val == "":
        print(f"[ERROR] Config option is empty: {opt}", file=sys.stderr)
        sys.exit(2)
    return val

# Returns an optional string; if missing, returns default (which may be None).
def opt_str(opt: str, default=None) -> str:
    """Fetch an OPTIONAL string from [delta], returning default if not set."""
    return cfg.get("delta", opt, fallback=default)

# Parse a boolean from config, failing if malformed when required.
def opt_bool(opt: str, default=None) -> bool:
    """Fetch a boolean-like option; accepts: true/false/yes/no/on/off/1/0."""
    s = cfg.get("delta", opt, fallback=None)
    if s is None:
        if default is None:
            print(f"[ERROR] Missing required boolean option: {opt}", file=sys.stderr)
            sys.exit(2)
        return default
    v = s.strip().lower()
    if v in ("1", "true", "yes", "on"):  return True
    if v in ("0", "false", "no", "off"): return False
    print(f"[ERROR] Invalid boolean value for {opt}: {s}", file=sys.stderr)
    sys.exit(2)

# Parse an integer from config, failing if missing/invalid when required.
def opt_int(opt: str, default=None) -> int:
    """Fetch an integer option."""
    s = cfg.get("delta", opt, fallback=None)
    if s is None:
        if default is None:
            print(f"[ERROR] Missing required integer option: {opt}", file=sys.stderr)
            sys.exit(2)
        return int(default)
    try:
        return int(s.strip())
    except ValueError:
        print(f"[ERROR] Invalid integer for {opt}: {s}", file=sys.stderr)
        sys.exit(2)

# Parse comma/newline separated list; comments (#...) and blanks are ignored.
def opt_list(opt: str, default=None) -> list:
    """Fetch a list (comma/newline separated); returns [] if empty and no default."""
    s = cfg.get("delta", opt, fallback=None)
    if s is None:
        if default is None:
            print(f"[ERROR] Missing required list option: {opt}", file=sys.stderr)
            sys.exit(2)
        s = default
    items = []
    for line in s.replace(",", "\n").splitlines():
        t = line.strip()
        if not t or t.startswith("#"):
            continue
        items.append(t)
    return items

# ===== CORE CONFIG (ALL required unless explicitly marked optional) =====
BACKUP_ROOT        = req("backup_root")                 # Root directory holding repositories
HOST_REPO          = req("host_repo")                   # Path to host repository
VM_REPO            = req("vm_repo")                     # Path to VM repository
HOST_PASSFILE      = req("host_passfile")               # Passfile for host repo (read with `cat`)
VM_PASSFILE        = req("vm_passfile")                 # Passfile for VM repo (read with `cat`)
HOST_EXCLUDES      = opt_list("host_excludes")          # List of excludes for host backup
EXTRA_PATHS        = opt_list("extra_paths", "")        # List of additional paths archived separately (can be empty)
EXTRA_PREFIX       = req("extra_prefix")                # Prefix used for extra paths (with hostname + index)
ENABLE_PRUNE       = opt_bool("enable_prune")           # Whether to prune after backups
PRUNE_KEEP_DAILY   = opt_int("prune_keep_daily")        # Retention: daily
PRUNE_KEEP_WEEKLY  = opt_int("prune_keep_weekly")       # Retention: weekly
PRUNE_KEEP_MONTHLY = opt_int("prune_keep_monthly")      # Retention: monthly
ENABLE_COMPACT     = opt_bool("enable_compact")         # Whether to compact repos after run
VM_SHUTDOWN_TIMEOUT= opt_int("vm_shutdown_timeout")     # Seconds to wait for graceful VM shutdown
VM_STARTUP_GRACE   = opt_int("vm_startup_grace")        # Seconds to wait after VM start
LOCK_FILE          = req("lock_file")                   # Lock-file path to serialize runs
LOCK_WAIT          = req("lock_wait")                   # Seconds to wait for repo lock
REQUIRE_MOUNTPOINT = opt_bool("require_mountpoint")     # Enforce mountpoint for BACKUP_ROOT
ENGINE_BIN         = req("engine_bin")                  # Underlying engine binary (e.g., borg)
ENGINE_COMPRESSION = req("engine_compression")          # Compression string (engine-native)
ENGINE_FILTER      = req("engine_filter")               # Filter (e.g., AME)
ENGINE_ONE_FS      = opt_bool("engine_one_file_system") # Restrict to one filesystem
ENGINE_FILES_CACHE = req("engine_files_cache")          # Files cache mode for engine

# -----------------------------------------------
# Utility helpers (no config duplication)
# -----------------------------------------------

# Print an error and exit with a code.
def die(msg, rc=1):
    """Print an error and exit with the given return code."""
    print(f"[ERROR] {msg}", file=sys.stderr)
    sys.exit(rc)

# Run a shell command with logging and optional environment overrides.
def run(cmd, check=True, capture_output=False, env=None):
    """Run a command, echo it, and return CompletedProcess."""
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)
    print(f"[CMD] {' '.join(shlex.quote(c) for c in cmd)}")
    return subprocess.run(cmd, check=check, capture_output=capture_output, text=True, env=env)

# Ensure the script is executed as root (required for system-wide reads).
def check_root():
    """Exit if not running as root."""
    if os.geteuid() != 0:
        die("Must be run as root.")

# Ensure backup root exists and (optionally) is a mountpoint.
def ensure_mount(path):
    """Validate that backup root exists and (optionally) is a mountpoint."""
    if not os.path.isabs(path):
        die(f"Backup root must be an absolute path: {path}")
    if not os.path.isdir(path):
        die(f"Backup root not found: {path}")
    if REQUIRE_MOUNTPOINT:
        if subprocess.run(["mountpoint", "-q", path]).returncode != 0:
            die(f"{path} is not a mountpoint")

# Set umask for group collaboration: group-writable, no world access.
def umask_group_rw():
    """Set umask to 007 (group writable, no access for others)."""
    os.umask(0o007)

# Build environment for the engine with non-interactive passphrase handling.
def delta_env(passfile):
    """Return environment for engine run (passcommand, cache, lock wait)."""
    e = os.environ.copy()
    e["BORG_PASSCOMMAND"] = f"cat {passfile}"          # engine expects this env name
    e["BORG_FILES_CACHE"] = ENGINE_FILES_CACHE         # config-driven cache policy
    e["BORG_LOCK_WAIT"]   = str(LOCK_WAIT)             # config-driven lock wait
    return e

# Create an archive for sources with optional excludes/comment.
def delta_create(repo, passfile, sources, excludes=None, prefix=None, comment=None):
    """Create an archive in repo from sources, applying excludes and metadata."""
    hostname = socket.gethostname()
    ts_local = time.strftime('%Y-%m-%d_%H-%M')
    archive  = f"{(prefix or hostname)}-{ts_local}"
    archive_loc = f"{repo}::{archive}"
    cmd = [
        ENGINE_BIN, "create",
        "--verbose", "--stats", "--show-rc", "--list",
        "--filter", ENGINE_FILTER,
        "--compression", ENGINE_COMPRESSION,
    ]
    if ENGINE_ONE_FS:
        cmd.append("--one-file-system")
    if comment:
        cmd.extend(["--comment", comment])
    else:
        cmd.extend(["--comment", f"Backup ({hostname}) {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}"])
    if excludes:
        for ex in excludes:
            cmd.extend(["--exclude", ex])
    cmd.append(archive_loc)
    cmd.extend(sources)
    print(f"[INFO] Creating delta archive: {archive_loc}")
    return run(cmd, check=False, env=delta_env(passfile)).returncode

# Prune archives according to retention policy for a given prefix.
def delta_prune(repo, passfile, prefix):
    """Prune old archives using retention policy and prefix."""
    cmd = [
        ENGINE_BIN, "prune",
        "--verbose", "--stats", "--show-rc",
        "--prefix", f"{prefix}-",
        "--keep-daily",   str(PRUNE_KEEP_DAILY),
        "--keep-weekly",  str(PRUNE_KEEP_WEEKLY),
        "--keep-monthly", str(PRUNE_KEEP_MONTHLY),
        repo,
    ]
    print(f"[INFO] Pruning delta archives with prefix '{prefix}-' in {repo}")
    return run(cmd, check=False, env=delta_env(passfile)).returncode

# Compact a repository to reclaim free space.
def delta_compact(repo, passfile):
    """Compact the repository to reclaim space."""
    print(f"[INFO] Compacting delta repo: {repo}")
    return run([ENGINE_BIN, "compact", "--progress", repo], check=False, env=delta_env(passfile)).returncode

# List all libvirt domains by name.
def list_domains():
    """Return a list of libvirt domain names (running or not)."""
    res = run(["virsh", "list", "--all", "--name"], check=False, capture_output=True)
    return [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]

# Get the current state string for a libvirt domain.
def domain_state(name):
    """Return the current state of a libvirt domain (lowercased)."""
    res = run(["virsh", "domstate", name], check=False, capture_output=True)
    return res.stdout.strip().lower()

# Determine whether the domain is considered running/active.
def domain_running(name):
    """Return True if the domain is running/idle/paused."""
    return domain_state(name) in ("running", "idle", "paused")

# Attempt graceful shutdown of a VM, then force if needed.
def domain_shutdown(name):
    """Request shutdown of domain; force stop after timeout if still running."""
    run(["virsh", "shutdown", name], check=False)
    deadline = time.time() + VM_SHUTDOWN_TIMEOUT
    while time.time() < deadline:
        time.sleep(3)
        if not domain_running(name):
            return True
    run(["virsh", "destroy", name], check=False)
    time.sleep(2)
    return not domain_running(name)

# Start a VM and wait briefly.
def domain_start(name):
    """Start domain and wait a short grace period."""
    run(["virsh", "start", name], check=False)
    time.sleep(VM_STARTUP_GRACE)

# Find disk image paths attached to a VM.
def domain_disk_paths(name):
    """Return a list of disk image file paths for a libvirt domain."""
    res = run(["virsh", "domblklist", "--details", name], check=False, capture_output=True)
    paths = []
    for ln in res.stdout.splitlines():
        parts = ln.split()
        if len(parts) >= 4 and parts[0] == "file" and parts[1] == "disk":
            src = parts[-1]
            if os.path.isabs(src) and os.path.exists(src):
                paths.append(src)
    if not paths:
        img_dir = "/var/lib/libvirt/images"
        if os.path.isdir(img_dir):
            for fn in os.listdir(img_dir):
                if name in fn:
                    paths.append(os.path.join(img_dir, fn))
    return paths

# Acquire a simple lock using a lockfile.
def acquire_lock():
    """Create a lock file to ensure only one run at a time."""
    fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)

# Release the lockfile if present.
def release_lock():
    """Remove the lock file (ignore if missing)."""
    try:
        os.unlink(LOCK_FILE)
    except FileNotFoundError:
        pass

# Validate that repos exist and passfiles are configured.
def require_repos_and_passfiles():
    """Verify repositories exist and passfiles are configured; exit if missing."""
    for path, label in ((HOST_REPO, "host_repo"), (VM_REPO, "vm_repo")):
        if not os.path.isdir(path):
            die(f"Repository not found: {path}")
    for pth, label in ((HOST_PASSFILE, "host_passfile"), (VM_PASSFILE, "vm_passfile")):
        if not os.path.isfile(pth):
            die(f"Passfile not found: {pth}")

# Orchestrate host + extra paths + VM backups with optional prune/compact.
def main():
    # Ensure we are root for system-wide access
    check_root()  # verify root privileges
    # Set cooperative permissions for group workflows
    umask_group_rw()  # set umask 007 (group-writable, no world)
    # Validate backup root and (optionally) mountpoint
    ensure_mount(BACKUP_ROOT)  # ensure root exists and is mounted if required
    # Validate repos and passfiles before proceeding
    require_repos_and_passfiles()  # exit early if repos or passfiles are missing

    # Acquire an exclusive run lock
    try:
        acquire_lock()  # create lockfile atomically
    except FileExistsError:
        die(f"Another run is active (lock: {LOCK_FILE})")  # refuse concurrent run

    try:
        # Cache hostname for archive naming
        host = socket.gethostname()  # get system hostname

        # === HOST BACKUP ===
        print("\n=== HOST DELTA BACKUP ===")  # section header for host backups
        rc_host = delta_create(  # create archive of root filesystem
            repo=HOST_REPO,                      # repository for host backups
            passfile=HOST_PASSFILE,              # passfile for host repository
            sources=["/"],                       # root filesystem
            excludes=HOST_EXCLUDES,              # exclusions (from conf)
            prefix=host,                         # archive prefix equals hostname
            comment=f"Host filesystem backup ({host})",  # descriptive comment
        )
        if rc_host not in (0, 1):  # 0 OK, 1 warnings
            print(f"[WARN] delta create for host returned {rc_host}")  # warn if unusual return code

        # === EXTRA PATHS (optional, separate archives per path) ===
        if EXTRA_PATHS:  # if any extra paths defined
            print("\n=== EXTRA PATHS BACKUP ===")  # section header for extra path backups
            for idx, path in enumerate(EXTRA_PATHS, 1):  # iterate with index
                if not os.path.exists(path):  # skip missing paths
                    print(f"[WARN] Extra path missing: {path}")  # warn on missing path
                    continue  # next path
                pref = f"{host}-{EXTRA_PREFIX}-{idx}"  # construct archive prefix for this path
                rc_extra = delta_create(  # create archive for the extra path
                    repo=HOST_REPO,                 # use host repository
                    passfile=HOST_PASSFILE,         # use host passfile
                    sources=[path],                 # single extra path
                    excludes=None,                  # no excludes for targeted path
                    prefix=pref,                    # path-specific prefix
                    comment=f"Extra path backup '{path}' ({host})",  # descriptive comment
                )
                if rc_extra not in (0, 1):  # verify status code
                    print(f"[WARN] delta create for extra path {path} returned {rc_extra}")  # warn if needed
        else:
            print("\n[INFO] No extra paths configured (extra_paths empty).")  # informative message

        # === RETENTION (optional) ===
        if ENABLE_PRUNE:  # retention enabled in config
            delta_prune(HOST_REPO, HOST_PASSFILE, prefix=host)  # prune host filesystem archives
            if EXTRA_PATHS:  # if there were extra path archives
                delta_prune(HOST_REPO, HOST_PASSFILE, prefix=f"{host}-{EXTRA_PREFIX}-")  # prune extra archives
        else:
            print("[INFO] Retention (prune) disabled for host repo.")  # note disabled

        # === SPACE RECLAMATION (optional) ===
        if ENABLE_COMPACT:  # compaction enabled in config
            delta_compact(HOST_REPO, HOST_PASSFILE)  # compact host repository

        # === VM BACKUPS ===
        print("\n=== VM DELTA BACKUPS ===")  # section header for VM backups
        domains = list_domains()  # list all libvirt domains
        if not domains:  # if there are no domains
            print("[INFO] No libvirt domains found.")  # info message
        for name in domains:  # iterate over domains
            print(f"\n--- VM: {name} ---")  # VM header
            was_running = domain_running(name)  # check if currently running
            if was_running:  # if active
                print(f"[INFO] Shutting down {name} ...")  # notify shutdown
                if not domain_shutdown(name):  # graceful shutdown or force
                    print(f"[WARN] {name} did not shut down cleanly; proceeding after force.")  # warn unclean stop
            else:
                print(f"[INFO] {name} is not running.")  # note VM was already stopped

            disks = domain_disk_paths(name)  # find disk image paths
            if not disks:  # none found
                print(f"[WARN] No disk paths found for {name}; skipping backup.")  # warn and skip
            else:
                print(f"[INFO] Disks for {name}: {', '.join(disks)}")  # list disks to be backed up
                rc_vm = delta_create(  # create archive for VM disks
                    repo=VM_REPO,                 # VM repository
                    passfile=VM_PASSFILE,         # VM passfile
                    sources=disks,                # disk files for this VM
                    prefix=f"{host}-{name}",      # archive prefix (host + vm)
                    comment=f"VM disk backup ({name} on {host})",  # descriptive comment
                )
                if rc_vm not in (0, 1):  # check result
                    print(f"[WARN] delta create for VM {name} returned {rc_vm}")  # warn if issues

            if was_running:  # if we had stopped it
                print(f"[INFO] Starting {name} ...")  # notify restart
                domain_start(name)  # start VM and wait briefly

        # === VM RETENTION (optional) ===
        if ENABLE_PRUNE:  # retention enabled
            delta_prune(VM_REPO, VM_PASSFILE, prefix=f"{host}-")  # prune VM archives
        else:
            print("[INFO] Retention (prune) disabled for VM repo.")  # note disabled

        # === VM SPACE RECLAMATION (optional) ===
        if ENABLE_COMPACT:  # compaction toggle
            delta_compact(VM_REPO, VM_PASSFILE)  # compact VM repository

        # === FINISH ===
        print("\n=== DONE ===")  # completion notice
    finally:
        # Always release the lock
        release_lock()  # remove lock file

# Entrypoint
if __name__ == "__main__":
    main()
