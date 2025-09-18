#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
delta-client-backup.py — curated user/workstation backup using centralized config
#################################################################################
Author: Max Haase – maxhaase@gmail.com
#################################################################################
Purpose:
  - Back up a selected set of important directories for a single machine/user.
  - All customization is centralized in: /etc/delta_backup.conf  (NOTE THE UNDERSCORE)
  - Minimal surface area: you don’t need to touch this script once config is set.

This script:
  - Reads /etc/delta_backup.conf (INI) for all settings.
  - Creates a timestamped archive in the configured repository.
  - Applies excludes (dev/build/browser cruft) inside SOURCES.
  - Optionally initializes the repository if missing.
  - Optionally prunes old archives (daily/weekly/monthly).
  - Supports dry run and verbose modes via environment variables.

Environment overrides (optional):
  DRY_RUN=1       -> Pass --dry-run to the engine (no writes)
  VERBOSE=1       -> Show verbose file listing
  DELTA_CONFIG=/path/to/custom.conf  -> Use an alternate config file

Passphrase handling:
  Preferred: set a passfile in config (client_passfile), which this script uses internally.
  Otherwise: export BORG_PASSPHRASE in your shell before running this script.

Under the hood:
  - Uses a deduplicating engine configured via the INI (e.g., engine_bin = borg).
  - The engine requires certain environment variables; the script sets those internally.
"""

import os
import shlex
import subprocess
import time
import socket
import sys
import pathlib
import configparser

# ===================== CONFIG LOADING =====================

def die(msg, rc=1):
    print(f"[ERROR] {msg}", file=sys.stderr)
    sys.exit(rc)

def info(msg):
    print(f"[INFO] {msg}")

def warn(msg):
    print(f"[WARN] {msg}")

def run(cmd, check=True, capture_output=False, env=None):
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)
    print(f"[CMD] {' '.join(shlex.quote(c) for c in cmd)}")
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture_output,
        text=True,
        env=env
    )

def expand(path: str) -> str:
    return os.path.abspath(os.path.expandvars(os.path.expanduser(path))) if path else path

def load_config():
    """
    Load /etc/delta_backup.conf (or DELTA_CONFIG) and return a dict of normalized settings.
    Expected keys (sections and defaults are permissive to avoid breaking older setups):

    [delta]                    # global engine + repo defaults used elsewhere in your project
      engine_bin = borg
      engine_compression = zstd,6
      engine_filter = AME
      engine_one_file_system = true
      engine_files_cache = ctime,size,inode
      lock_wait = 120

    [client]                   # THIS script’s section (curated sources backup)
      client_repo = user@host:/path/to/repo                     (REQUIRED)
      client_passfile = /home/user/.config/delta/client.pass    (optional; else use BORG_PASSPHRASE)
      client_ssh_key = /home/user/.ssh/delta_backup             (optional; falls back to default ssh)
      client_sources = /home/user/Documents,/home/user/Projects (comma-separated; REQUIRED)
      client_excludes = pattern1,pattern2,...                   (comma-separated; optional)
      prune_enable = true|false                                 (default: true)
      prune_keep_daily = 7
      prune_keep_weekly = 4
      prune_keep_monthly = 6
      init_repo = true|false                                    (default: true)
    """
    cfg_path = expand(os.environ.get("DELTA_CONFIG", "/etc/delta_backup.conf"))
    if not os.path.isfile(cfg_path):
        die(f"Config file not found: {cfg_path}")

    cp = configparser.ConfigParser()
    cp.read(cfg_path)

    def get(sec, key, default=None):
        try:
            return cp.get(sec, key)
        except Exception:
            return default

    def getbool(sec, key, default=False):
        v = get(sec, key, None)
        if v is None:
            return default
        return str(v).strip().lower() in ("1", "yes", "true", "on")

    # [delta] engine defaults
    engine_bin          = get("delta", "engine_bin", "borg")
    engine_compression  = get("delta", "engine_compression", "zstd,6")
    engine_filter       = get("delta", "engine_filter", "AME")
    engine_one_fs       = getbool("delta", "engine_one_file_system", True)
    engine_files_cache  = get("delta", "engine_files_cache", "ctime,size,inode")
    lock_wait           = str(get("delta", "lock_wait", "120"))

    # [client] curated backup
    client_repo         = get("client", "client_repo", None)
    client_passfile     = get("client", "client_passfile", "")
    client_ssh_key      = get("client", "client_ssh_key", "")
    client_sources      = get("client", "client_sources", "")
    client_excludes     = get("client", "client_excludes", "")
    prune_enable        = getbool("client", "prune_enable", True)
    prune_keep_daily    = str(get("client", "prune_keep_daily", "7"))
    prune_keep_weekly   = str(get("client", "prune_keep_weekly", "4"))
    prune_keep_monthly  = str(get("client", "prune_keep_monthly", "6"))
    init_repo           = getbool("client", "init_repo", True)

    # Normalize lists
    def split_csv(val):
        if not val:
            return []
        # support commas or newlines
        raw = []
        for part in str(val).split(","):
            raw.extend(part.splitlines())
        return [p.strip() for p in raw if p.strip()]

    sources  = [expand(p) for p in split_csv(client_sources)]
    excludes = split_csv(client_excludes)

    # Expand paths
    client_repo     = client_repo.strip() if client_repo else None
    client_passfile = expand(client_passfile) if client_passfile else ""
    client_ssh_key  = expand(client_ssh_key)  if client_ssh_key  else ""

    if not client_repo:
        die("Missing [client].client_repo in config. Please set it in /etc/delta_backup.conf")
    if not sources:
        die("Missing [client].client_sources in config. Please set at least one source path.")

    return {
        "cfg_path": cfg_path,
        "engine_bin": engine_bin,
        "engine_compression": engine_compression,
        "engine_filter": engine_filter,
        "engine_one_file_system": engine_one_fs,
        "engine_files_cache": engine_files_cache,
        "lock_wait": lock_wait,
        "client_repo": client_repo,
        "client_passfile": client_passfile,
        "client_ssh_key": client_ssh_key,
        "sources": sources,
        "excludes": excludes,
        "prune_enable": prune_enable,
        "keep": {
            "daily": prune_keep_daily,
            "weekly": prune_keep_weekly,
            "monthly": prune_keep_monthly,
        },
        "init_repo": init_repo,
    }

# ===================== ENGINE ENV BUILDERS =====================

def engine_env(cfg, use_passfile=True):
    """
    Build environment for the underlying engine.
    - If passfile provided, set BORG_PASSCOMMAND to 'cat <file>'.
    - Else rely on BORG_PASSPHRASE exported by user.
    - Apply engine cache + lock wait from config.
    - If SSH key is set, configure BORG_RSH accordingly.
    """
    e = os.environ.copy()

    # Passphrase via passfile (preferred) or existing env
    if use_passfile and cfg["client_passfile"]:
        if not os.path.isfile(cfg["client_passfile"]):
            die(f"Passfile not found: {cfg['client_passfile']}")
        e["BORG_PASSCOMMAND"] = f"cat {cfg['client_passfile']}"
    else:
        if "BORG_PASSPHRASE" not in e and "BORG_PASSCOMMAND" not in e:
            die("No pass provided. Set [client].client_passfile in config OR export BORG_PASSPHRASE in your shell.")

    # Engine cache & lock wait
    e["BORG_FILES_CACHE"] = str(cfg["engine_files_cache"])
    e["BORG_LOCK_WAIT"]   = str(cfg["lock_wait"])

    # SSH command if a key is provided (repo over SSH)
    if cfg["client_repo"] and ":" in cfg["client_repo"]:  # rudimentary SSH repo detection
        ssh_cmd = ["ssh", "-o", "StrictHostKeyChecking=no"]
        if cfg["client_ssh_key"]:
            if os.path.isfile(cfg["client_ssh_key"]):
                ssh_cmd += ["-i", cfg["client_ssh_key"]]
            else:
                warn(f"SSH key {cfg['client_ssh_key']} not found — using default SSH identity")
        e["BORG_RSH"] = " ".join(ssh_cmd)
        e["BORG_REMOTE_PATH"] = cfg["engine_bin"]

    return e

# ===================== ENGINE COMMANDS =====================

def repo_exists(cfg) -> bool:
    """Return True if repository is accessible."""
    try:
        run([cfg["engine_bin"], "info", cfg["client_repo"]], env=engine_env(cfg), check=True)
        return True
    except subprocess.CalledProcessError:
        return False

def initialize_repo_if_needed(cfg):
    """Initialize repository if missing and init_repo is enabled."""
    if repo_exists(cfg):
        return
    if not cfg["init_repo"]:
        die("Repository does not exist and [client].init_repo is false. Aborting.")
    info("Initializing new repository...")
    run([cfg["engine_bin"], "init", "--encryption=repokey-blake2", cfg["client_repo"]],
        env=engine_env(cfg), check=True)

def create_backup(cfg) -> int:
    """Create a curated backup archive of configured sources."""
    hostname = socket.gethostname()
    archive  = f"{hostname}-{time.strftime('%Y-%m-%d_%H-%M')}"
    eb       = cfg["engine_bin"]

    cmd = [
        eb, "create",
        "--stats", "--show-rc",
        "--compression", cfg["engine_compression"],
        "--exclude-caches",
        "--comment", f"Curated backup of {hostname} - {time.strftime('%Y-%m-%d %H:%M')}",
    ]

    if cfg["engine_one_file_system"]:
        cmd.append("--one-file-system")

    # Optional verbose listing
    if os.environ.get("VERBOSE", "0") == "1":
        cmd += ["--verbose", "--list", "--filter", cfg["engine_filter"]]

    # Optional dry run
    if os.environ.get("DRY_RUN", "0") == "1":
        cmd.append("--dry-run")

    # Excludes applied *inside* the SOURCES
    for ex in cfg["excludes"]:
        cmd.extend(["--exclude", ex])

    # Target + sources (only existing ones)
    sources = [p for p in cfg["sources"] if pathlib.Path(p).exists()]
    if not sources:
        die("No valid sources found to back up — check [client].client_sources in /etc/delta_backup.conf")

    cmd.append(f"{cfg['client_repo']}::{archive}")
    cmd += sources

    return run(cmd, check=False, env=engine_env(cfg)).returncode

def prune_backups(cfg) -> int:
    """Apply retention policy if enabled."""
    if not cfg["prune_enable"]:
        info("Prune disabled by config.")
        return 0
    eb       = cfg["engine_bin"]
    hostname = socket.gethostname()
    cmd = [
        eb, "prune",
        "--verbose", "--stats", "--show-rc",
        "--prefix", f"{hostname}-",
        cfg["client_repo"],
    ]
    keep = cfg["keep"]
    for k in ("daily", "weekly", "monthly"):
        if keep.get(k):
            cmd.extend([f"--keep-{k}", str(keep[k])])
    return run(cmd, check=False, env=engine_env(cfg)).returncode

# ===================== MAIN =====================

def main():
    cfg = load_config()

    info(f"=== Starting curated backup of {socket.gethostname()} ===")
    info(f"Config: {cfg['cfg_path']}")
    info(f"Repo:   {cfg['client_repo']}")
    info(f"Time:   {time.strftime('%Y-%m-%d %H:%M:%S')}")

    # Initialize repo if needed
    initialize_repo_if_needed(cfg)

    # Create backup
    rc = create_backup(cfg)
    if rc not in (0, 1):  # 0=success, 1=warnings
        die(f"Backup failed with return code {rc}", rc)

    # Prune
    prc = prune_backups(cfg)
    if prc != 0:
        warn(f"Prune operation returned {prc}")

    info("=== Backup completed ===")

if __name__ == "__main__":
    main()
