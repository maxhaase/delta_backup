** delta-backup **

**delta-backup** is a small, opinionated backup orchestrator for Linux. Unlike conventional backup systems, instead of taking huge amounts of storage, it behaves more like a versioning system, where even **binary deltas** for huge and small binary files, which makes it very easy to maintain and recover file versions across time. 
It backs up your **host filesystem**, optional **extra paths**, and **libvirt/KVM VM disks**.

- For servers and workstations, especially those that host virtual machines
- Easy to configure, install and forget (sweet!)
- Secure, it is all encrypted - Note: if you lose your password, there is no way to recover from a total system failure!  
- Single source of truth: **`/etc/delta-backup.conf`**  
- Safe orchestration (VM shutdown → backup → restart)  
- Optional retention (prune) and compaction  
- Locking to prevent concurrent runs  

Under the hood, delta-backup uses a proven backup engine (Borg) for deduplication and repositories, while keeping its own workflow and UX.

---

## Contents

- `delta_backup.py` – the orchestrator (must run as root)  
- `delta_backup_install.sh` – installer that reads `./delta-backup.conf` (from repo) and installs it to `/etc/delta-backup.conf`  
- `delta-backup.conf` – **the** configuration (centralized; used by *all* scripts)  
- `systemd/delta-backup.service` – one-shot service (reads the same conf)  
- `systemd/delta-backup.timer` – schedule (e.g., nightly)  

Author: **Max Haase – maxhaase@gmail.com**

---

**1) Install**

Clone the repo and run the installer as root:

```bash
git clone https://github.com/maxhaase/delta-backup.git
cd delta-backup
sudo ./delta_backup_install.sh
The installer takes delta-backup.conf from the repo and installs it to /etc/delta-backup.conf.

It will not overwrite an existing /etc/delta-backup.conf unless you set DELTA_FORCE=1.

Edit /etc/delta-backup.conf to match your environment, then re-run the installer.

**2) Configure**
Edit /etc/delta-backup.conf. Every line is documented. Example:

[delta]
backup_root = /srv/backup
host_repo   = /srv/backup/host-backup
vm_repo     = /srv/backup/vm-backup

host_passfile = /home/delta/.config/delta/host.pass
vm_passfile   = /home/delta/.config/delta/vm.pass

host_excludes = /proc,/sys,/dev,/run,/tmp,/var/tmp,/lost+found,/mnt,/media,/SWAPFILE,/var/lib/libvirt/images,/var/cache,/var/lib/apt/lists,/var/cache/apt/archives,*/.cache
extra_paths =
extra_prefix = extra

enable_prune = false
prune_keep_daily = 7
prune_keep_weekly = 4
prune_keep_monthly = 6

enable_compact = true
vm_shutdown_timeout = 300
vm_startup_grace = 5

lock_file = /var/lock/delta-backup.lock
lock_wait = 120
require_mountpoint = false

engine_bin = borg
engine_compression = zstd,6
engine_filter = AME
engine_one_file_system = true
engine_files_cache = ctime,size,inode

delta_user = delta
delta_group = backup

**3) Run on demand**
sudo ./delta_backup.py
If your config is in a non-default path:

sudo DELTA_CONFIG=/path/to/your.conf ./delta_backup.py
4) Automate with systemd
Install the service and timer:

sudo install -D -m 0755 delta_backup.py /usr/local/bin/delta_backup.py
sudo install -D -m 0644 systemd/delta-backup.service /etc/systemd/system/delta-backup.service
sudo install -D -m 0644 systemd/delta-backup.timer   /etc/systemd/system/delta-backup.timer

sudo systemctl daemon-reload
sudo systemctl enable --now delta-backup.timer
Check logs:

systemctl status delta-backup.timer
journalctl -u delta-backup.service -n 200 --no-pager
Change schedule (delta-backup.timer defaults to 2:00 AM):

OnCalendar=*-*-* 02:00:00
Reload:

sudo systemctl daemon-reload
sudo systemctl restart delta-backup.timer
5) Restore
Restoring Virtual Machines
bash
Copy code
# List VM backups
BORG_PASSCOMMAND='cat /home/delta/.config/delta/vm.pass' borg list /srv/backup/vm-backup

# Extract a VM disk image
cd /
BORG_PASSCOMMAND='cat /home/delta/.config/delta/vm.pass' borg extract --progress /srv/backup/vm-backup::HOST-VM-YYYY-MM-DD_HH-MM var/lib/libvirt/images/VM.qcow2

# Restore libvirt XML (if needed)
BORG_PASSCOMMAND='cat /home/delta/.config/delta/host.pass' borg extract /srv/backup/host-backup::LATEST etc/libvirt/qemu/VM.xml

# Re-define and start the VM
virsh define /etc/libvirt/qemu/VM.xml
virsh start VM
Full System Restoration (Disaster Recovery)
Reinstall Linux (e.g., Debian or Ubuntu).

Create user delta (or the one you use for backups).

Install required packages:

sudo apt update
sudo apt install -y borgbackup qemu-kvm libvirt-daemon-system
Mount your backup disk:

sudo mount /dev/sdX1 /srv/backup
Verify repositories:

BORG_PASSCOMMAND='cat /home/delta/.config/delta/host.pass' borg list /srv/backup/host-backup
BORG_PASSCOMMAND='cat /home/delta/.config/delta/vm.pass'   borg list /srv/backup/vm-backup
Restore host configs:

cd /
BORG_PASSCOMMAND='cat /home/delta/.config/delta/host.pass' borg extract /srv/backup/host-backup::LATEST etc
Restore VM disks and XMLs (see above).

Recreate cron or systemd schedules.

Reboot and verify system.

6) Cheatsheet
Run the following as root:

bash
Copy code
# Mount the USB disk that contains the backup repositories
mount /dev/sdX1 /srv/backup

# List all archives in the host repository
BORG_PASSCOMMAND='cat /home/delta/.config/delta/host.pass' borg list /srv/backup/host-backup

# Show info about a specific archive
BORG_PASSCOMMAND='cat /home/delta/.config/delta/host.pass' borg info /srv/backup/host-backup::ARCHIVE

# Extract a single file from host backup
BORG_PASSCOMMAND='cat /home/delta/.config/delta/host.pass' borg extract /srv/backup/host-backup::ARCHIVE etc/ssh/sshd_config

# Mount a backup archive for browsing (read-only)
mkdir -p /mnt/borg
BORG_PASSCOMMAND='cat /home/delta/.config/delta/host.pass' borg mount /srv/backup/host-backup::LATEST /mnt/borg
umount /mnt/borg

# Restore a VM disk
BORG_PASSCOMMAND='cat /home/delta/.config/delta/vm.pass' borg extract /srv/backup/vm-backup::ARCHIVE var/lib/libvirt/images/VM.qcow2

# Define and start a VM
virsh define /etc/libvirt/qemu/VM.xml && virsh start VM

# Check repository consistency
BORG_PASSCOMMAND='cat /home/delta/.config/delta/host.pass' borg check /srv/backup/host-backup

# Compact repository
BORG_PASSCOMMAND='cat /home/delta/.config/delta/host.pass' borg compact /srv/backup/host-backup

# Prune old backups (use with caution!)
BORG_PASSCOMMAND='cat /home/delta/.config/delta/host.pass' borg prune --keep-daily=7 --keep-weekly=4 --keep-monthly=6 /srv/backup/host-backup
7) Linux Basics (for newcomers)
Download Linux ISO: Debian or Ubuntu.

Create bootable USB: use balenaEtcher or dd on Linux.

Install Linux: follow the installer, create a user (e.g., delta).

Add user to sudoers:

sudo usermod -aG sudo delta
Open a terminal: usually Ctrl+Alt+T.

Switch to root:

sudo -i
Edit crontab (not needed if using systemd):

crontab -e
Check logs:

tail -f /var/log/syslog

**FAQ**
Where do I configure paths, repos, and exclusions?
Only in /etc/delta-backup.conf.

Do I need to run Borg commands directly?
Not for regular backups. delta-backup orchestrates everything. You may use Borg directly for restores.

Why root?
To read all system files and control libvirt VMs.

Where should I store backups?
On a separate disk or device. Never on the same disk as your OS.

License
MIT – see LICENSE.

© Author: Max Haase – maxhaase@gmail.com
Why root?
To read all files and control libvirt VMs.
