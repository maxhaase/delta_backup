#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================
# Project: delta-backup orchestrator
# Author: Max Haase – maxhaase@gmail.com
# ===============================================
"""
delta_backup.py — Config-driven host + VM backups with live libvirt snapshots.

Reads settings from /etc/delta_backup.conf [delta].
- Host backup via Borg with excludes from config
- VM backups: external snapshots (disk-only, atomic), backup base images while guest runs on overlays,
  then blockcommit/pivot to merge overlays (no shutdowns).
- If QEMU guest agent is available: use --quiesce (application-consistent).
- If not: briefly virsh suspend/resume around snapshot (crash-consistent, tiny pause).

Dependencies: borg, virsh (libvirt), qemu-img, mountpoint, findmnt
"""

import configparser
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time

# ---------------------- tiny util ----------------------
def die(msg, rc=1):
    print(f"[ERROR] {msg}", file=sys.stderr); sys.exit(rc)

def info(msg):  print(f"[INFO]  {msg}")
def warn(msg):  print(f"[WARN]  {msg}", file=sys.stderr)

def run(cmd, check=True, capture_output=False, env=None):
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)
    print(f"[CMD] {' '.join(shlex.quote(c) for c in cmd)}")
    return subprocess.run(cmd, check=check, capture_output=capture_output, text=True, env=env)

def need_cmd(name):
    if shutil.which(name) is None:
        die(f"Missing command: {name}")

def as_bool(v, default=False):
    if v is None:
        return default
    s = str(v).strip().lower()
    return s in ("1","y","yes","true","on")

def split_list(s):
    """
    Split comma/newline separated lists; strip spaces; drop empties.
    """
    if not s:
        return []
    out = []
    for part in str(s).replace("\r","").replace("\t"," ").split(","):
        for piece in part.split("\n"):
            p = piece.strip()
            if p:
                out.append(p)
    return out

# ---------------------- config ----------------------
def load_config(path="/etc/delta_backup.conf"):
    if not os.path.isfile(path):
        die(f"Config not found: {path}")
    cfgp = configparser.ConfigParser(
        interpolation=None,
        inline_comment_prefixes=("#",";"),
        strict=False
    )
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfgp.read_file(f)
    except Exception as e:
        die(f"Failed to read {path}: {e}")

    if "delta" not in cfgp:
        die(f"Missing [delta] section in {path}")

    c = cfgp["delta"]

    cfg = {}
    # Paths
    cfg["backup_root"]    = c.get("backup_root", "/STORE/BACKUP/")
    cfg["host_repo"]      = c.get("host_repo",   os.path.join(cfg["backup_root"], "host-backup"))
    cfg["vm_repo"]        = c.get("vm_repo",     os.path.join(cfg["backup_root"], "vm-backup"))
    cfg["host_passfile"]  = c.get("host_passfile", "")
    cfg["vm_passfile"]    = c.get("vm_passfile",   "")

    # Lists
    cfg["host_excludes"]  = split_list(c.get("host_excludes", ""))
    cfg["extra_paths"]    = split_list(c.get("extra_paths", ""))  # not used here but parsed

    # Flags / retention
    cfg["enable_prune"]   = as_bool(c.get("enable_prune", "false"))
    cfg["prune_keep_daily"]   = int(c.get("prune_keep_daily", "7"))
    cfg["prune_keep_weekly"]  = int(c.get("prune_keep_weekly", "4"))
    cfg["prune_keep_monthly"] = int(c.get("prune_keep_monthly","6"))
    cfg["enable_compact"] = as_bool(c.get("enable_compact","true"))

    # VM timing / safety
    cfg["vm_shutdown_timeout"] = int(c.get("vm_shutdown_timeout","600"))  # not used (no shutdowns)
    cfg["vm_startup_grace"]    = int(c.get("vm_startup_grace","5"))

    # Locking / mount
    cfg["lock_file"]           = c.get("lock_file", "/var/lock/delta-backup.lock")
    cfg["lock_wait"]           = str(int(c.get("lock_wait", "120")))
    cfg["require_mountpoint"]  = as_bool(c.get("require_mountpoint","false"))

    # Engine knobs (borg)
    cfg["engine_bin"]          = c.get("engine_bin","borg")
    cfg["engine_compression"]  = c.get("engine_compression","zstd,6")
    cfg["engine_filter"]       = c.get("engine_filter","AME")
    cfg["engine_one_file_system"] = as_bool(c.get("engine_one_file_system","true"))
    cfg["engine_files_cache"]  = c.get("engine_files_cache","ctime,size,inode")

    # Internal defaults
    cfg["overlay_dir"]         = "/var/lib/libvirt/images"

    return cfg

# ---------------------- borg helpers ----------------------
def borg_env(cfg, passfile):
    e = os.environ.copy()
    if passfile:
        if not os.path.isfile(passfile):
            die(f"Passfile not readable: {passfile}")
        e["BORG_PASSCOMMAND"] = f"cat {passfile}"
    if cfg["engine_files_cache"]:
        e["BORG_FILES_CACHE"] = cfg["engine_files_cache"]
    if cfg["lock_wait"]:
        e["BORG_LOCK_WAIT"] = cfg["lock_wait"]
    return e

def borg_create(cfg, repo, passfile, sources, excludes=None, prefix=None, comment=None):
    hostname = socket.gethostname()
    archive = f"{(prefix or hostname)}-{time.strftime('%Y-%m-%d_%H-%M')}"
    archive_loc = f"{repo}::{archive}"

    cmd = [
        cfg["engine_bin"], "create",
        "--verbose","--stats","--show-rc","--list",
        "--filter", cfg["engine_filter"],
        "--compression", cfg["engine_compression"],
    ]
    if cfg["engine_one_file_system"]:
        cmd.append("--one-file-system")
    if comment:
        cmd += ["--comment", comment]
    if excludes:
        for ex in excludes:
            cmd += ["--exclude", ex]

    cmd.append(archive_loc)
    cmd += sources
    rc = run(cmd, check=False, env=borg_env(cfg, passfile)).returncode
    return rc

def borg_prune(cfg, repo, passfile, prefix):
    cmd = [
        cfg["engine_bin"], "prune",
        "--verbose","--stats","--show-rc",
        "--prefix", f"{prefix}-",
        repo
    ]
    for k, v in (("daily", cfg["prune_keep_daily"]),
                 ("weekly", cfg["prune_keep_weekly"]),
                 ("monthly", cfg["prune_keep_monthly"])):
        cmd += [f"--keep-{k}", str(v)]
    return run(cmd, check=False, env=borg_env(cfg, passfile)).returncode

def borg_compact(cfg, repo, passfile):
    return run([cfg["engine_bin"], "compact", "--progress", repo],
               check=False, env=borg_env(cfg, passfile)).returncode

# ---------------------- libvirt helpers ----------------------
ACTIVE_STATES  = {"running","idle","blocked","pmsuspended","in shutdown"}
PAUSED_STATE   = "paused"

def dom_list():
    r = run(["virsh","list","--all","--name"], check=False, capture_output=True)
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]

def dom_state(name):
    r = run(["virsh","domstate",name], check=False, capture_output=True)
    line = r.stdout.splitlines()[0] if r.stdout else ""
    return line.strip().lower()

def is_running(name):  return dom_state(name) in ACTIVE_STATES
def is_paused(name):   return dom_state(name) == PAUSED_STATE

def suspend(name):
    run(["virsh","suspend",name], check=False)
    # Wait briefly for paused state
    for _ in range(50):
        if is_paused(name):
            return True
        time.sleep(0.1)
    return is_paused(name)

def resume(name, grace_sec):
    run(["virsh","resume",name], check=False)
    time.sleep(grace_sec)

def qga_available(name, timeout=2):
    try:
        r = run(["virsh","qemu-agent-command", name, '{"execute":"guest-ping"}', f"--timeout={timeout}"],
                check=False, capture_output=True)
        return r.returncode == 0
    except Exception:
        return False

def domblk_targets(name):
    """
    Return list of (target, source_path) for disks.
    """
    r = run(["virsh","domblklist","--details",name], check=False, capture_output=True)
    pairs = []
    for ln in r.stdout.splitlines():
        parts = ln.split()
        if len(parts) >= 5 and parts[1] == "disk":
            pairs.append((parts[2], parts[-1]))
    return pairs

def qemu_img_info_json(path):
    r = run(["qemu-img","info","--output=json", path], check=False, capture_output=True)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except Exception:
        return None

def create_external_snapshot(name, use_quiesce):
    snap_name = f"delta-{name}-{time.strftime('%Y%m%d-%H%M%S')}"
    args = ["virsh","snapshot-create-as","--domain",name,"--name",snap_name,"--disk-only","--atomic"]
    if use_quiesce:
        args.append("--quiesce")
    r = run(args, check=False)
    if r.returncode != 0:
        die(f"Snapshot create failed for {name} (quiesce={use_quiesce})")
    return snap_name

def bases_from_overlays(after_pairs):
    """
    From domain block map AFTER snapshot, extract base images via qemu-img backing-filename.
    """
    bases = []
    overlays = []
    for tgt, overlay in after_pairs:
        if not os.path.isabs(overlay):
            continue
        info = qemu_img_info_json(overlay)
        if info:
            backing = info.get("backing-filename")
            if backing:
                if not os.path.isabs(backing):
                    backing = os.path.abspath(os.path.join(os.path.dirname(overlay), backing))
                if os.path.exists(backing):
                    bases.append(backing)
        overlays.append(overlay)
    # De-duplicate while keeping order
    seen = set(); bases = [x for x in bases if not (x in seen or seen.add(x))]
    seen = set(); overlays = [x for x in overlays if not (x in seen or seen.add(x))]
    return bases, overlays

def blockcommit_pivot(name, targets):
    for tgt in targets:
        run(["virsh","blockcommit",name,tgt,"--active","--verbose","--pivot"], check=False)

# ---------------------- lock + mount ----------------------
def acquire_lock(path):
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.write(fd, str(os.getpid()).encode()); os.close(fd)
        return True
    except FileExistsError:
        return False

def release_lock(path):
    try: os.unlink(path)
    except FileNotFoundError: pass

def ensure_mountpoint(cfg):
    if cfg["require_mountpoint"]:
        need_cmd("findmnt")
        r = run(["findmnt","-rno","TARGET", cfg["backup_root"]], check=False, capture_output=True)
        if r.returncode != 0:
            die(f"require_mountpoint=true but {cfg['backup_root']} is not a mountpoint.")
    # Always ensure /STORE is mounted if you rely on it (optional hardening)
    if os.path.isdir("/STORE"):
        if run(["mountpoint","-q","/STORE"], check=False).returncode != 0:
            die("/STORE is not mounted (safety check).")

# ---------------------- main backup flow ----------------------
def backup_host(cfg):
    host = socket.gethostname()
    info("=== HOST BACKUP ===")
    rc_host = borg_create(
        cfg, cfg["host_repo"], cfg["host_passfile"],
        sources=["/"],
        excludes=cfg["host_excludes"],
        prefix=host,
        comment=f"Host filesystem backup ({host})"
    )
    if rc_host not in (0,1):
        warn(f"borg create for host returned {rc_host}")
    # Example: extra paths could be separate archives (not used unless wanted)
    # if cfg["extra_paths"]:
    #     for idx, p in enumerate(cfg["extra_paths"], 1):
    #         borg_create(cfg, cfg["host_repo"], cfg["host_passfile"],
    #                     sources=[p], excludes=None,
    #                     prefix=f"{host}-extra{idx}",
    #                     comment=f"Extra path backup ({p}) on {host}")

    if cfg["enable_prune"]:
        borg_prune(cfg, cfg["host_repo"], cfg["host_passfile"], prefix=host)
    else:
        info("Retention (prune) disabled for host repo.")
    if cfg["enable_compact"]:
        borg_compact(cfg, cfg["host_repo"], cfg["host_passfile"])

def backup_vms_live(cfg):
    host = socket.gethostname()
    info("=== VM BACKUPS (live external snapshots) ===")
    vms = dom_list()
    if not vms:
        info("No libvirt domains found.")
        return
    os.makedirs(cfg["overlay_dir"], exist_ok=True)

    for name in vms:
        print()
        info(f"--- VM: {name} ---")
        paused_here = False
        try:
            has_qga = qga_available(name)
            info(f"QEMU guest agent: {'available' if has_qga else 'not available'}")

            # If no agent, pause briefly to capture consistent snapshot window
            if not has_qga:
                info(f"Pausing {name} briefly for snapshot …")
                if suspend(name):
                    paused_here = True
                else:
                    warn(f"Could not confirm paused state for {name}; continuing.")

            before = domblk_targets(name)
            snap_name = create_external_snapshot(name, use_quiesce=has_qga)
            info(f"Created snapshot: {snap_name}")

            # Resume immediately if we paused
            if paused_here:
                resume(name, cfg["vm_startup_grace"])
                paused_here = False

            after = domblk_targets(name)
            bases, overlays = bases_from_overlays(after)
            if not bases:
                warn("Could not determine base images from overlays; falling back to current sources.")
                bases = [src for (_, src) in after if os.path.isabs(src) and os.path.exists(src)]

            info("Base images: " + (", ".join(bases) if bases else "(none)"))
            info("Overlay images: " + (", ".join(overlays) if overlays else "(none)"))

            # Backup bases while VM runs on overlays
            rc_vm = borg_create(
                cfg, cfg["vm_repo"], cfg["vm_passfile"],
                sources=bases,
                excludes=None,
                prefix=f"{host}-{name}",
                comment=f"VM disk backup (external snapshot base) for {name} on {host}"
            )
            if rc_vm not in (0,1):
                warn(f"borg create for VM {name} returned {rc_vm}")

            # Merge overlays back and pivot
            targets = [t for (t, _) in after]
            info("Blockcommit/pivot overlays: " + (", ".join(targets) if targets else "(none)"))
            if targets:
                blockcommit_pivot(name, targets)

            # Cleanup leftover overlay files (libvirt usually unlinks on pivot; best-effort)
            for ov in overlays:
                try:
                    if os.path.exists(ov):
                        os.unlink(ov)
                except Exception as e:
                    warn(f"Could not delete overlay {ov}: {e}")

        finally:
            if paused_here:
                try:
                    resume(name, cfg["vm_startup_grace"])
                except Exception:
                    pass

    if cfg["enable_prune"]:
        borg_prune(cfg, cfg["vm_repo"], cfg["vm_passfile"], prefix=f"{host}-")
    else:
        info("Retention (prune) disabled for VM repo.")
    if cfg["enable_compact"]:
        borg_compact(cfg, cfg["vm_repo"], cfg["vm_passfile"])

# ---------------------- entry ----------------------
def main():
    if os.geteuid() != 0:
        die("Must be run as root.")
    for tool in ("borg","virsh","qemu-img","mountpoint"):
        need_cmd(tool)

    conf_path = os.environ.get("DELTA_CONF", "/etc/delta_backup.conf")
    cfg = load_config(conf_path)

    # Sanity
    for p in (cfg["host_repo"], cfg["vm_repo"]):
        if not os.path.isdir(p):
            die(f"Repository not found: {p}")

    if not acquire_lock(cfg["lock_file"]):
        die(f"Another run is active (lock: {cfg['lock_file']})")
    try:
        ensure_mountpoint(cfg)

        # HOST BACKUP
        backup_host(cfg)

        # VM BACKUPS (live snapshots)
        backup_vms_live(cfg)

        info("=== DONE ===")
    finally:
        release_lock(cfg["lock_file"])

# ----------------------
if __name__ == "__main__":
    main()

