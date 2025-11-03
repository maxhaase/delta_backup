
## delta-backup - install and forget! 

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

Author: **Max Haase – maxhaase@gmail.com**
---
---

## System Requirements

### If this is a VM host (with KVM/libvirt)
- A Linux distribution (Debian, Ubuntu, Fedora, etc.)
- Packages: `qemu-kvm`, `libvirt-daemon-system`
- Sufficient storage for VM disk backups
- A **separate disk or storage device** for backups (strongly recommended)

### If this is a standalone Linux machine (no VMs)
- A Linux distribution (Debian, Ubuntu, etc.)
- Package: `borgbackup`
- A **separate disk or storage device** for backups

> **Common sense**: Do **not** keep backups on the same disk as your operating system.  
> If the OS disk fails or is corrupted, you can lose both system and backups. Put backups on a different disk/device.

---

## Repository Contents

- `delta_backup.py` — the orchestrator (must run as root)  
- `delta_backup_install.sh` — installer; installs `delta-backup.conf` into **`/etc/delta-backup.conf`**  
- `delta-backup.conf` — the configuration file (centralized; used by all scripts)  
- `systemd/delta-backup.service` — one-shot service (reads the same conf)  
- `systemd/delta-backup.timer` — schedule (e.g., nightly)

---

## 1) Install

Clone the repo and run the installer as root:

~~~bash
git clone https://github.com/maxhaase/delta-backup.git
cd delta-backup
sudo ./delta_backup_install.sh
~~~

- The installer takes `delta-backup.conf` from the repo and installs it to `/etc/delta-backup.conf`.  
- It will not overwrite an existing `/etc/delta-backup.conf` unless you set `DELTA_FORCE=1`.  
- Edit `/etc/delta-backup.conf` to match your environment, then re-run the installer.

---

## 2) Configure

Edit `/etc/delta-backup.conf`. Every line is documented. Example:

~~~ini
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
~~~

---

## 3) Run on demand

~~~bash
sudo ./delta_backup.py
~~~

If your config is in a non-default path:

~~~bash
sudo DELTA_CONFIG=/path/to/your.conf ./delta_backup.py
~~~

---

## 4) Automate with systemd

Install the service and timer:

~~~bash
sudo install -D -m 0755 delta_backup.py /usr/local/bin/delta_backup.py
sudo install -D -m 0644 systemd/delta-backup.service /etc/systemd/system/delta-backup.service
sudo install -D -m 0644 systemd/delta-backup.timer   /etc/systemd/system/delta-backup.timer

sudo systemctl daemon-reload
sudo systemctl enable --now delta-backup.timer
~~~

Check logs:

~~~bash
systemctl status delta-backup.timer
journalctl -u delta-backup.service -n 200 --no-pager
~~~

Change schedule (`delta-backup.timer` defaults to 2:00 AM):

~~~ini
OnCalendar=*-*-* 02:00:00
~~~

Reload:

~~~bash
sudo systemctl daemon-reload
sudo systemctl restart delta-backup.timer
~~~

---

## 5) Restore

### Restoring Virtual Machines

~~~bash
# List VM backups
BORG_PASSCOMMAND='cat /home/delta/.config/delta/vm.pass' borg list /srv/backup/vm-backup

# Extract a VM disk image (example: VM name "XSOL")
cd /
BORG_PASSCOMMAND='cat /home/delta/.config/delta/vm.pass' borg extract --progress /srv/backup/vm-backup::HOST-XSOL-YYYY-MM-DD_HH-MM var/lib/libvirt/images/XSOL.qcow2

# Restore libvirt XML (if needed) from host backup
BORG_PASSCOMMAND='cat /home/delta/.config/delta/host.pass' borg extract /srv/backup/host-backup::LATEST etc/libvirt/qemu/XSOL.xml

# Re-define and start the VM
virsh define /etc/libvirt/qemu/XSOL.xml
virsh start XSOL
~~~

### Full System Restoration (Disaster Recovery)

1) Reinstall your Linux distro (Debian/Ubuntu recommended) and create user `delta`.  
2) Install required packages:

~~~bash
sudo apt update
sudo apt install -y borgbackup qemu-kvm libvirt-daemon-system
~~~

3) Mount your backup disk:

~~~bash
sudo mount /dev/sdX1 /srv/backup
~~~

4) Verify repositories:

~~~bash
BORG_PASSCOMMAND='cat /home/delta/.config/delta/host.pass' borg list /srv/backup/host-backup
BORG_PASSCOMMAND='cat /home/delta/.config/delta/vm.pass'   borg list /srv/backup/vm-backup
~~~

5) Restore host configs:

~~~bash
cd /
BORG_PASSCOMMAND='cat /home/delta/.config/delta/host.pass' borg extract /srv/backup/host-backup::LATEST etc
~~~

6) Restore VM disk images and XMLs (see VM section above).  
7) Recreate cron or systemd schedules.  
8) Reboot and verify system.

---

## 6) Cheatsheet (Quick Reference)

Run the following as root.

~~~bash
# Mount the USB/disk that contains the backup repositories
mount /dev/sdX1 /srv/backup

# List all backup archives in the host repository
BORG_PASSCOMMAND='cat /home/delta/.config/delta/host.pass' borg list /srv/backup/host-backup

# Show detailed info about a specific host backup archive
BORG_PASSCOMMAND='cat /home/delta/.config/delta/host.pass' borg info /srv/backup/host-backup::ARCHIVE

# List all backup archives in the VM repository
BORG_PASSCOMMAND='cat /home/delta/.config/delta/vm.pass' borg list /srv/backup/vm-backup

# Show detailed info about a specific VM backup archive
BORG_PASSCOMMAND='cat /home/delta/.config/delta/vm.pass' borg info /srv/backup/vm-backup::ARCHIVE

# Extract a single file from host backup (example: sshd_config)
BORG_PASSCOMMAND='cat /home/delta/.config/delta/host.pass' borg extract /srv/backup/host-backup::ARCHIVE etc/ssh/sshd_config

# Mount a host backup archive for browsing (read-only)
mkdir -p /mnt/borg
BORG_PASSCOMMAND='cat /home/delta/.config/delta/host.pass' borg mount /srv/backup/host-backup::LATEST /mnt/borg
umount /mnt/borg

# Extract a VM disk image from backup
BORG_PASSCOMMAND='cat /home/delta/.config/delta/vm.pass' borg extract /srv/backup/vm-backup::ARCHIVE var/lib/libvirt/images/VM.qcow2

# Define and start a VM from restored configuration
virsh define /etc/libvirt/qemu/VM.xml && virsh start VM

# Check repository integrity and consistency
BORG_PASSCOMMAND='cat /home/delta/.config/delta/host.pass' borg check /srv/backup/host-backup

# Compact repository to free space (after prune)
BORG_PASSCOMMAND='cat /home/delta/.config/delta/host.pass' borg compact /srv/backup/host-backup

# Prune old backups according to retention policy (use with caution!)
BORG_PASSCOMMAND='cat /home/delta/.config/delta/host.pass' borg prune --keep-daily=7 --keep-weekly=4 --keep-monthly=6 /srv/backup/host-backup

# Show repository compression and deduplication statistics
BORG_PASSCOMMAND='cat /home/delta/.config/delta/host.pass' borg info /srv/backup/host-backup

# Extract entire directory from backup
BORG_PASSCOMMAND='cat /home/delta/.config/delta/host.pass' borg extract /srv/backup/host-backup::ARCHIVE home/delta/Documents

# Search for files in backup archives
BORG_PASSCOMMAND='cat /home/delta/.config/delta/host.pass' borg list /srv/backup/host-backup::ARCHIVE | grep filename

# Check disk usage of backup repositories
du -h /srv/backup
~~~

---

## 7) Linux Basics (New User Notes)

- **Download Linux ISO**  
  - Debian: https://www.debian.org/download  
  - Ubuntu: https://ubuntu.com/download
- **Create bootable USB**  
  - GUI: https://etcher.balena.io/  
  - CLI (advanced): `dd if=your.iso of=/dev/sdX bs=4M status=progress oflag=sync`
- **Install Linux** and create a user (e.g., `delta`).
- **Add the user to sudoers**:

~~~bash
sudo usermod -aG sudo delta
~~~

- **Open a terminal**: usually `Ctrl+Alt+T`.
- **Become root** for a session:

~~~bash
sudo -i
~~~

- **Edit crontab** (not needed if using systemd timers):

~~~bash
crontab -e
~~~

- **Check logs**:

~~~bash
tail -f /var/log/syslog
~~~

- **For VM hosts**: ensure the user can manage libvirt/KVM:

~~~bash
sudo usermod -aG libvirt,kvm delta
newgrp libvirt
~~~

---

## FAQ

**Where do I configure paths, repos, and exclusions?**  
Only in `/etc/delta-backup.conf`.

## MOUNT A BACKUP AND BROWSE THROUGH THE FILES WITHOUT HARMING ANYTHING :-) 
* First, create a place to mount it, for example:
  ` sudo mkdir -p /mnt/host-backup`
* Then mount it!
`sudo BORG_PASSCOMMAND='cat /home/max/.config/borg/host-backup.pass' borg mount /STORE/BACKUP/host-backup::<LABEL> /mnt/host-backup`

You get your <LABEL> of which backup you want to browse, by listing the backups on you have, like this command, the column in the left is the <LABEL>:

root@sun:/# BORG_PASSCOMMAND='cat /home/max/.config/borg/vm-backup.pass' borg list /STORE/BACKUP/vm-backup 
`SUN-XSOL-2025-09-08_17-06            Mon, 2025-09-08 17:07:11 [03bbf11dced4174c6d0c583ddd19ae68fd0977f27a64c173c800c746d9ccf7fc]
SUN-XSOL-2025-09-08_18-27            Mon, 2025-09-08 18:27:52 [0035420812ee84e54455487d3054f3f3d05c2c913acd7e5e6b2178b389a9bbd6]
SUN-XSOL-2025-09-08_18-41            Mon, 2025-09-08 18:41:37 [6a366bf794cee871cafe689243f6a0fec6d89caef91a321acdeaace0178fe465]
SUN-XSOL-2025-09-08_22-06            Mon, 2025-09-08 22:06:09 [417be4aa51f375c32fd2ce9d1b333ba80406997a1519b9c48df771fc6f3e4692]
SUN-XSOL-2025-09-08_22-15            Mon, 2025-09-08 22:15:49 [965c1a733258c70fd2a3d29071bc3c3f3185479999d606f8fc9d8654d299ef87]
SUN-XSOL-2025-09-10_00-03            Wed, 2025-09-10 00:03:02 [2e515bdfd2dccd00b2d10d67ec0da3b390c31acb5638750467f9e66593e69bdf]
SUN-XSOL-2025-09-10_03-00            Wed, 2025-09-10 03:00:48 [68d8ba06ee441a5f8031c1abb1b17c526903087072d9bdc4c5b1dbfb8d61b05a]
SUN-XSOL-2025-09-10_13-38            Wed, 2025-09-10 13:38:47 [c9ae6e7e0d3814ff9492fd27585c98e0566c7d7ceabedc58175849676fc0193a]
SUN-XSOL-2025-09-10_14-18            Wed, 2025-09-10 14:18:07 [6d9e4236562b81abb0c6300e450f165faeeb095c94ecb9840587c7482ffc082a]
SUN-XSOL-2025-09-11_03-01            Thu, 2025-09-11 03:01:40 [01d832b215d4cd5b29c6e8beaff8c99f65c9657adda5d7b713ce96b721c3b167]
SUN-XSOL-2025-09-11_14-24            Thu, 2025-09-11 14:24:04 [cc7a099bb6ef3e7f35408dc53233e74c1cc30aba053606cb6e653401903b1d7f]
SUN-XSOL-2025-09-12_03-01            Fri, 2025-09-12 03:01:33 [39b1806b37f347e70168b214b00c2d66d275faadf00cad1366f782550f942c16]
SUN-XSOL-2025-09-13_03-09            Sat, 2025-09-13 03:09:30 [9704807e328230e0af57b1c21af6a3c9eb9c15b18d7f4c99670d18b2f43655a0]
SUN-XSOL-2025-09-15_22-00            Mon, 2025-09-15 22:00:53 [8c96fe5849d5a9cab9eb7c54b90b83812c270ef2f92fde48d5ac606734986398]
SUN-XSOL-2025-09-16_03-00            Tue, 2025-09-16 03:00:55 [e3c027810287437da3e38781690fc400273c6dd5cd3e3be00ccdcf08efc4c16c]
SUN-XSOL-2025-09-17_03-00            Wed, 2025-09-17 03:00:53 [47e17db5646b11ad1bc2899d02dec2e89c5f0f481dd036c5f93b8c5a462aab52]
SUN-XSOL-2025-09-17_12-02            Wed, 2025-09-17 12:02:32 [e03b57440edda7435b7bf5099bc86e6bb56085fc2a2cc489dbe0ca10986482ea]
SUN-XSOL-2025-09-17_19-19            Wed, 2025-09-17 19:19:38 [f3328be47fbc53015b36e2c564330a672aa5e33d280895149e3d8f832fe25a82]
SUN-XSOL-2025-09-18_00-08            Thu, 2025-09-18 00:08:57 [fd4fbbcff3aba4468b1955dc8a7477141d24a2b761bfc66c0e4c3ae5127ae678]
SUN-XSOL-2025-09-18_03-00            Thu, 2025-09-18 03:00:44 [7839355ac64f86357704e6b56b97c7728cb5338b0558835b7f315322ca0d554c]
SUN-XSOL-2025-09-19_03-00            Fri, 2025-09-19 03:00:51 [e5c87486bec21fb9b1b4d03843f5abdc4ad5cd7c57b882977389efe5c9843aa0]
SUN-XSOL-2025-09-20_03-00            Sat, 2025-09-20 03:00:56 [dfc3f31a44a72054899b703a8502b17f8137ae7d14bfacf13d162143689149f1]
SUN-XSOL-2025-09-21_03-00            Sun, 2025-09-21 03:00:56 [5dc403b9096529027c27bdda31862124514301b4ed16aeacc279c156bd12ff80]
SUN-XSOL-2025-09-22_03-00            Mon, 2025-09-22 03:00:49 [83a37feec2b74a3bc07960c9f3666eed2dfb6e50fce11e134bd6dcd6d194b386]
SUN-XSOL-2025-09-23_03-00            Tue, 2025-09-23 03:00:51 [2c8550b33780c9b1f6e485e342b4341372c8ce7358498d717339b3eb188f5a23]
SUN-XSOL-2025-09-24_03-00            Wed, 2025-09-24 03:00:49 [e15f34cf593848f2065d2798c75b93c25cb3444f6fdcfd594aa80703d36e5867]
SUN-XSOL-2025-09-25_03-00            Thu, 2025-09-25 03:00:43 [5c8e93f883d4032ce620eddd8840be39bd3b0a1f149e7e1c7f56bca897e77cad]
SUN-XSOL-2025-09-26_03-00            Fri, 2025-09-26 03:00:36 [91ed1379a5afa542589ac9cab77a803241912f51c78f845cc848e1e99bbff4b7]
SUN-XSOL-2025-09-27_03-00            Sat, 2025-09-27 03:00:50 [b91c59013c53ffda0906f34ca4b72ebc2ac9814eaf4c7441b74f79e008a2a31b]
SUN-XSOL-2025-09-27_23-36            Sat, 2025-09-27 23:36:52 [3430986f06a1dbf271cf921beb8680658584ac37af03c02a9d8b097d967a2bfb]
SUN-XSOL-2025-09-28_03-01            Sun, 2025-09-28 03:01:26 [39dc1944a7084ce81a17c2e622d1e56c9f82ca124e1d6aa5889fc5c1733efa15]
SUN-XSOL-2025-09-29_03-01            Mon, 2025-09-29 03:01:24 [5598b8ab3e88f1d0df2ef85a440c05b504c3e94e5ba93899b10748e3b16fd7b2]
SUN-XSOL-2025-09-30_03-01            Tue, 2025-09-30 03:01:21 [f7b87376bdce922aeb148391e8c54de3befb107ba9394577161645b6e1013fc0]
SUN-XSOL-2025-10-01_03-01            Wed, 2025-10-01 03:01:23 [10c9104be5b459c26f76e2e6ab318426a6029df42d88dbd4ed67a00ff427bf1a]
SUN-XSOL-2025-10-02_03-01            Thu, 2025-10-02 03:01:34 [dde9914a7f06de369ab24d260f81a9f4a27f0b25621a96cda5b359720203f85e]
SUN-XSOL-2025-10-03_03-04            Fri, 2025-10-03 03:04:46 [a215fa9c4a29eaaee3d0b47b8a649786676d0577a75fccd2a74fbca6872b1f66]
SUN-XSOL-2025-10-03_20-17            Fri, 2025-10-03 20:17:01 [20755eaa4c210090f63e81416e3d8c994d7bb276a940d2c01481c249dffd6f74]
SUN-XSOL-2025-10-06_00-57            Mon, 2025-10-06 00:57:14 [f65497242f0f796ff46ce824a82fc81f56e26d6b033067d232d1edee02e482b4]
SUN-XSOL-2025-10-07_00-27            Tue, 2025-10-07 01:27:14 [bcefacf6560d1875d63d07237902c162c7e7684bcecac3c6878ad080fa224c47]
SUN-XSOL-2025-10-07_02-41            Tue, 2025-10-07 03:41:14 [3a5f88054d534dc30b230789f5d1dabfa984bb16efdda70997b8774a8e65c27e]
SUN-XSOL-2025-10-10_22-26            Fri, 2025-10-10 23:26:50 [f6ba43873f6aa9fbbf1e6a1ee0ccf114a5157228a63b19e28f519828f3b37b5a]
SUN-XSOL-2025-10-11_02-45            Sat, 2025-10-11 03:45:02 [049915e76b3dcb45e48be9387156d8ebbe166b974c2d2da134931806b077e9c0]
SUN-XSOL-2025-10-11_22-33            Sat, 2025-10-11 23:34:00 [b54e3536ddd7a7b8928675e0a76fc53c4817137cf3a2b55600bc1e4addf6a5f0]
SUN-XSOL-2025-10-17_15-02            Fri, 2025-10-17 16:03:00 [c47c6c60ac48d3facf175004f477a9b77bf337c4c098a7c361bd056424642733]`

* So, to mount label SUN-XSOL-2025-10-17_15-02, you would:
`sudo BORG_PASSCOMMAND='cat /home/max/.config/borg/host-backup.pass' borg mount /STORE/BACKUP/host-backup::SUN-XSOL-2025-10-17_15-02 /mnt/host-backup`

** To browse easily with a GUI file manager, I recommend the Midnight Commander, you install it like `apt install mc` or `yum install mc`, depending on your system**

**Do I need to run Borg directly?**  
Not for routine backups. delta-backup orchestrates the runs. For restores, using Borg directly is fine.

**Why root?**  
To read all system files and control libvirt VMs.

**Where should I store backups?**  
On a separate disk or device. Never on the same disk as your OS.

---

## License

MIT – see `LICENSE`.

© Author: **Max Haase – maxhaase@gmail.com**
