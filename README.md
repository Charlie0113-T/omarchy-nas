# Omarchy NAS 0.3.0

**English** · [简体中文](#中文)

Plug a USB drive enclosure into a computer running [Omarchy](https://github.com/omacom/omarchy) and share the data volumes already on its disks with your home network over SMB.

It is a standalone community tool, not affiliated with Omarchy, and it doesn't rely on unpublished Omarchy APIs. To see the shares in the Omarchy bar, add the [Omarchy NAS bar plugin](https://github.com/Charlie0113-T/omarchy_nas).

This version covers: disk scanning, guided setup, read-only and writable shares, a NAS password, bringing registered volumes back after a reboot or re-plug, pause and resume, and a local status page.

> [!NOTE]
> **Status: in use.** As of 2026-10-29, omarchy-nas has been running stably on a real Omarchy machine for two weeks.

> [!NOTE]
> The setup wizard, command output and status page are in Chinese only for now. The commands and options themselves are in English, as below.

## Requirements

- Omarchy (Arch Linux) with systemd, and Python 3.11 or later.
- `samba`; `ntfs-3g` for NTFS volumes; `apfs-fuse` for APFS volumes (see [APFS drives](#apfs-drives)).
- Data volumes on a disk Linux sees as USB or removable. The system disk is never offered.

## Install

```bash
sudo pacman -S --needed samba ntfs-3g
git clone https://github.com/Charlie0113-T/omarchy-nas.git
cd omarchy-nas
python3 build.py                      # writes dist/omarchy-nas.pyz
python3 dist/omarchy-nas.pyz setup
```

Setup asks for sudo, then walks you through picking the home network interface, the external data volume and a share name. It shows the capacity, volume identity and read/write mode for you to confirm, then asks for a separate NAS password. APFS needs an explicit volume number.
Shares are **read-only** by default. Writing is enabled only with `--writable`, and only on file systems that support it.

Setup copies the program to `/opt/omarchy-nas/` and installs the `omarchy-nas` command. When it's done:

- Local status page: <http://127.0.0.1:8787>
- The terminal prints the real `smb://host-IP/share-name`; go by that output.
- Mac: Finder → Go → Connect to Server.
- iPad: Files → ⋯ → Connect to Server → Registered User.
- Windows: type `\\host-IP\share-name` in File Explorer's address bar.
- Sign in with the Linux user name setup showed and **the NAS password you just set**. That password lives in omarchy-nas's own Samba account database; your Linux login password doesn't change.

The status page shows state and the commands for each action. Copy a command into a terminal on the host to run it; the page itself holds no root rights and offers no remote disk management.
Shares accept only the home private IPv4 network you chose. omarchy-nas doesn't touch your router or firewall: if the firewall blocks it, allow TCP 445 for that network only.

## Look before installing

```bash
python3 dist/omarchy-nas.pyz scan
python3 dist/omarchy-nas.pyz doctor
python3 dist/omarchy-nas.pyz serve --demo
```

`scan` and `doctor` don't change any disk. `serve --demo` previews the status page with clearly labelled made-up data; it mounts nothing, creates no share and changes nothing on the host.
Without `--demo`, `serve` reads the real local state and must run as a normal user.

## File systems

| File system | Default | Writable | Notes |
|---|---|---|---|
| APFS | read-only | not offered | needs apfs-fuse; mounted by explicit volume number; one volume per container |
| ext4 | read-only | on request | read-only uses `noload`, so the journal isn't replayed; existing POSIX permissions are kept |
| XFS | read-only | on request | read-only uses `norecovery`; existing POSIX permissions are kept |
| exFAT / FAT | read-only | on request | mapped to the chosen normal user |
| NTFS | read-only | on request | needs ntfs-3g; never forces a repair or removes a Windows hibernation file |
| Btrfs / LUKS / LVM / software RAID / HFS+ | detected and reported | not implemented | avoids wrong guesses about multi-device or encrypted layouts |

This version shares standalone data volumes that Linux shows as USB or removable. Any enclosure brand can work, but firmware, USB bridge chips and hardware RAID haven't been checked one by one.
It shares only volumes the system can see and doesn't replace a hardware RAID tool.
The system disk and partitions on it are excluded; a volume another program has mounted must be unmounted there first.
It doesn't format, partition, repair, decrypt, join Fusion Drives, do Time Machine, NFS or exposure to the internet. APFS is never written.
If every data disk is APFS and you want a writable share, you can shrink an APFS container on a Mac and create an ext4 partition by hand. Do it before `setup`: registered volumes are identified partly by their size.

## APFS drives

What this version does for APFS:

1. Recognises APFS GPT partition types, even before `lsblk` shows an FSTYPE; uses the UUID when there is one, otherwise the PARTUUID.
2. On first use, shows the container's details from `apfsutil` (when installed) and makes you pick the APFS volume number; it never assumes the first volume holds the data.
3. Mounts with the chosen normal user's UID/GID, `allow_other`, read-only and its own FUSE subtype, and checks the mount source before sharing.
4. Gives each APFS/NTFS FUSE process its own transient systemd service, so the periodic check can't take the mount down with it when it exits.
5. Keeps errors in the terminal, the systemd journal and the status page; a volume that didn't mount is never shown as mounted.

apfs-fuse's own documentation says it is a read-only driver that doesn't support every compression method or firmlinks. This version has no passphrase flow for encrypted volumes and doesn't try to recover damaged containers.
So when you first check an APFS volume, open a few existing files (text, photos, video) and compare them with known copies on a Mac. Listing a directory doesn't prove every file reads correctly.

Check for the drivers first:

```bash
command -v apfs-fuse
command -v apfsutil
```

If you built apfs-fuse yourself and the binary is still in its build directory, install the executable you trust to `/usr/local/bin/`. Don't run unknown scripts as root just to get past the check.
Without a driver, follow the upstream build instructions: <https://github.com/sgan81/apfs-fuse>. omarchy-nas doesn't bundle or replace third-party drivers.

Choosing an APFS volume explicitly (use your real device path, volume number and network interface):

```bash
python3 dist/omarchy-nas.pyz setup --device /dev/sdc2 --name family --apfs-volume 0
```

Don't copy the example device path; the current path in the `scan` output is what counts.

## Commands

After setup, the `omarchy-nas` command is installed:

```bash
omarchy-nas status                  # live status as JSON
omarchy-nas doctor                  # dependency, disk and network checks
sudo omarchy-nas add                # register another external volume, read-only by default
sudo omarchy-nas add --writable     # ask for a writable share
sudo omarchy-nas add --writable --claim-root   # new empty ext4/XFS volume: hand only its root directory to the share user
sudo omarchy-nas pause family       # stop sharing and unmount cleanly; not during a transfer
sudo omarchy-nas resume family      # check the volume again and remount it
sudo omarchy-nas remove family      # unregister and unmount; files on the volume stay
sudo omarchy-nas passwd             # change the NAS password
sudo omarchy-nas stop               # disable the services and autostart, unmount, keep the config
sudo omarchy-nas start              # enable again
sudo omarchy-nas network            # re-register after the router, network or home interface changed
```

Pausing or removing a share restarts omarchy-nas's own Samba process, which briefly drops connections to the other shares too. Finish transfers first.
Unplug USB only after the unmount succeeded; if one disk holds several registered volumes, pause each of them. When a busy volume can't be unmounted, nothing forces or lazily unmounts it, and the disk is never declared safe to unplug.
After a re-plug, registered volumes that are **still enabled** are retried every 20 seconds. A volume you paused stays paused until `resume`.
A stale mount left by a sudden disconnect is reported as a stale mount, with the `pause` / `resume` commands to fix it; it isn't forced off. ext4/XFS are checked by device number and APFS by the device node used at mount time, so a re-plugged disk isn't mistaken for online. Run `pause` then `resume`, or restart normally.

When the default service already holds port 8787, there's no need to run `serve` again: just open the status page. For an extra temporary page use `serve --port 8788`.

## Update

```bash
cd omarchy-nas
git pull
python3 build.py
sudo python3 dist/omarchy-nas.pyz upgrade
```

`upgrade` installs the new build and its service files and restarts the running services; registered volumes and the NAS password stay as they are.

## Remove

1. Run `sudo omarchy-nas stop`. It disables the services and autostart and unmounts every registered volume. If it reports a busy volume, stop here and don't unplug the disk.
2. Delete what setup installed:

   ```bash
   sudo rm -f /etc/systemd/system/omarchy-nas-*.service /etc/systemd/system/omarchy-nas-*.timer
   sudo systemctl daemon-reload
   sudo rm -rf /opt/omarchy-nas /etc/omarchy-nas /var/lib/omarchy-nas /run/omarchy-nas
   sudo rm -f /usr/local/bin/omarchy-nas
   sudo sh -c 'rmdir /srv/omarchy-nas/* /srv/omarchy-nas'   # empty mount points only
   ```

   `rmdir` only removes empty directories, so it refuses any volume that is still mounted.
3. Remove the firewall rule you added for port 445: `sudo ufw status numbered`, then `sudo ufw delete <number>`.

Files on your data disks are never touched.

## Where things live

- Program: `/opt/omarchy-nas/omarchy-nas.pyz`
- Command: `/usr/local/bin/omarchy-nas`
- Registered volumes: `/etc/omarchy-nas/config.json`
- Its own Samba configuration: `/etc/omarchy-nas/smb.conf`
- Its own Samba state and accounts: `/var/lib/omarchy-nas/`
- Mount root: `/srv/omarchy-nas/`
- Status cache: `/run/omarchy-nas/status.json`
- systemd services: `omarchy-nas-smb.service`, `omarchy-nas-panel.service`, `omarchy-nas-reconcile.service` / `.timer`
- Transient FUSE services: `omarchy-nas-volume-<share>.service`

## Boundaries

omarchy-nas doesn't modify `/etc/fstab`, an existing `/etc/samba/smb.conf`, sudoers or the firewall. Omarchy enables UFW and denies inbound connections by default, so allow 445 (SMB) for your home network only; `doctor` prints the command. If it finds a Samba service already running, setup stops instead of taking it over.
Samba listens only on loopback and the registered home interface (`interfaces = lo <interface>`, `bind interfaces only = yes`) and accepts connections only from 127.0.0.1 and the registered home IPv4 network; everything else, public IPv6 included, is refused. The status page service runs in a systemd sandbox (ProtectHome, ProtectSystem=full, PrivateTmp and more) and can't reach home directories or docker.sock.
A volume mounts automatically only after you registered it once by hand; newly plugged devices are never shared on their own.
Volumes are checked by UUID/PARTUUID, capacity, file system and, when available, the disk's serial number. Duplicate UUIDs from cloned disks are refused.
Empty mount points are owned by root with mode 000, and Samba checks the real mount on every new connection; symlinks aren't followed.
Read-only by default doesn't make a damaged disk safe to experiment on: read errors are reported as they are, and fsck or forced recovery never runs on its own.

## Troubleshooting

```bash
omarchy-nas doctor
omarchy-nas status
systemctl status omarchy-nas-smb omarchy-nas-panel omarchy-nas-reconcile
journalctl -u omarchy-nas-reconcile -n 60 --no-pager
```

- Mount point used by another program: unmount it in that program first.
- Not enough file system permissions: existing permissions are kept, and nothing runs a recursive chown/chmod. This version shares the whole volume root, so a writable volume needs a root directory the share user can write. Create new ext4 volumes with `mkfs.ext4 -E root_owner=$(id -u):$(id -g)`; for an empty existing volume (only `lost+found`), add `--claim-root` to `add --writable`, which changes only the volume root. For a volume that already holds data, decide on permissions yourself.
- APFS volume won't mount: check the volume number, encryption, Fusion Drive and the driver version; don't switch to writable or lax modes to get past the error.
- `testparm` fails: the old configuration stays and the new share doesn't start.
- First setup failed after saving the config: run `sudo omarchy-nas start` to recover; don't wipe the data volume.
- Connection refused: check the host IP printed, the chosen network interface, the home network and the firewall (`sudo ufw status`). The status page's localhost address isn't the file share address for another machine. If the network or interface changed, run `sudo omarchy-nas network`.

## Verification

52 automated tests cover disk registration and mount guards, APFS staying read-only, the Samba configuration, the systemd sandbox and the status page. As of 2026-10-29, omarchy-nas has been running stably on a real Omarchy machine for two weeks.

## Source and development

The `.pyz` is a standard ZIP holding the Python source, the web page, the tests and this README; no pip or npm needed.

```bash
python3 -m unittest discover -s tests -v
python3 build.py
```

To read the source of a `.pyz` you were given: `python3 -m zipfile -e omarchy-nas.pyz omarchy-nas-src`.
It targets Python 3.11+ on Omarchy / Arch Linux with systemd; install paths on other distributions are untested.

References:

- APFS driver and its limits: <https://github.com/sgan81/apfs-fuse>
- Samba configuration: <https://www.samba.org/samba/docs/current/man-html/smb.conf.5.html>
- Samba password tool: <https://www.samba.org/samba/docs/current/man-html/smbpasswd.8.html>
- lsblk: <https://man.archlinux.org/man/lsblk.8.en>
- mount: <https://man.archlinux.org/man/mount.8.en>

## License

Released under the [GNU Affero General Public License v3.0](LICENSE). Copyright © 2026 Charles Tao. This is a community tool and is not affiliated with Omarchy.

---

## 中文

把 USB 硬盘柜接到运行 Omarchy 的电脑，用已有数据卷提供家庭 SMB 共享。
这是独立社区工具，与 Omarchy 官方无关，不依赖未公开的 Omarchy 插件 API。想在 Omarchy 顶栏看共享状态，可以装 [Omarchy NAS 顶栏插件](https://github.com/Charlie0113-T/omarchy_nas)。

本版交付：设备扫描、交互设置、只读/可写共享、访问密码、重启/重新插入后恢复已登记卷、暂停/恢复，以及本机状态页。

> [!NOTE]
> **当前状态：已在使用。** 截至 2026-10-29，omarchy-nas 已在一台真实的 Omarchy 电脑上稳定运行两周。

### 需要

- Omarchy（Arch Linux）+ systemd，Python 3.11 或更新。
- `samba`；NTFS 卷需要 `ntfs-3g`；APFS 卷需要 `apfs-fuse`（见下方 APFS 一节）。
- 数据卷所在的硬盘在 Linux 里显示为 USB 或可移除设备。系统盘永远不会出现在选项里。

### 安装

```bash
sudo pacman -S --needed samba ntfs-3g
git clone https://github.com/Charlie0113-T/omarchy-nas.git
cd omarchy-nas
python3 build.py                      # 生成 dist/omarchy-nas.pyz
python3 dist/omarchy-nas.pyz setup
```

设置向导会申请 sudo 权限，依次让你选择家庭网络接口、外接数据卷、共享名；显示容量、卷标识和读写模式后确认，再单独设置 NAS 密码。APFS 需要明确输入卷编号。
默认共享为**只读**。仅在支持的文件系统上，明确加 `--writable` 才启用写入。

完成后：

- 本机管理页：<http://127.0.0.1:8787>
- 终端打印真实 `smb://主机IP/共享名`，以实际输出为准。
- Mac：Finder → 前往 → 连接服务器。
- iPad：文件 → ⋯ → 连接服务器 → 注册用户。
- Windows：文件资源管理器地址栏输入 `\\主机IP\共享名`。
- 使用向导显示的 Linux 用户名和**刚设置的 NAS 密码**。NAS 密码存放在本工具自己的 Samba 账户库，不改 Linux 登录密码。

管理页提供状态和操作命令。复制命令到主机终端后执行；网页本身不持有 root 权限，不提供远程磁盘管理接口。
本版共享只允许所选家庭私有 IPv4 网段，不自动调整路由器或防火墙。若防火墙拦截，只为这个网段允许 TCP 445。

### 先查看，不安装

```bash
python3 dist/omarchy-nas.pyz scan
python3 dist/omarchy-nas.pyz doctor
python3 dist/omarchy-nas.pyz serve --demo
```

`scan` / `doctor` 不改磁盘。`serve --demo` 是明确标注的虚构数据预览，不会挂载、建立共享或改变主机。
不用 `--demo` 时读取真实本机状态，须用普通用户运行。

### 文件系统范围

| 文件系统 | 默认 | 可写 | 说明 |
|---|---|---|---|
| APFS | 只读 | 不提供 | 需 apfs-fuse；按明确卷编号挂载；单容器一个卷 |
| ext4 | 只读 | 显式启用 | 只读加 noload，不重放日志；保留已有 POSIX 权限 |
| XFS | 只读 | 显式启用 | 只读加 norecovery；保留已有 POSIX 权限 |
| exFAT / FAT | 只读 | 显式启用 | 映射到所选普通用户 |
| NTFS | 只读 | 显式启用 | 需 ntfs-3g；不强制修复或移除 Windows 休眠文件 |
| Btrfs / LUKS / LVM / 软件 RAID / HFS+ | 识别并提示 | 未实现 | 避免对多设备或加密结构做错误推断 |

本版只接入 Linux 显示为 USB 或可移除的独立数据卷。硬盘柜的品牌不限，但固件、USB 桥接芯片和硬件 RAID 的兼容性没有逐一验证。
仅提供系统能识别的卷，不代替硬件 RAID 管理工具。
系统盘及其同盘分区被排除；已由其他程序挂载的卷需要先正常卸载。
程序不处理格式化、分区、修复、自动解密、Fusion Drive 拼接、Time Machine、NFS 或公网穿透。APFS 不提供写入。
数据盘全是 APFS、又需要可写共享时，可以在 Mac 上缩小 APFS 容器，再手动新建一个 ext4 分区。要在 `setup` 之前完成：已登记的卷按容量核对身份。

### APFS 硬盘

本版对 APFS 做了这些处理：

1. APFS GPT 分区类型识别（lsblk 尚未显示 FSTYPE 时也能提示）；有 UUID 用 UUID，没有则用 PARTUUID。
2. 首次接入显示 `apfsutil` 容器信息（该工具已安装时），明确选择 APFS 卷编号；不默选“第一个就是数据卷”。
3. 指定普通用户 UID/GID、allow_other、只读及独立 FUSE subtype，核验挂载来源后才共享。
4. APFS/NTFS 的 FUSE 进程由独立的 systemd transient service 持有，避免定时检查退出时顺手杀掉挂载进程。
5. 错误会保留在终端、systemd 日志以及状态页中；没有挂上时不会标成成功。

apfs-fuse 上游明确说明：它是只读驱动，未支持所有压缩方法，也不支持 firmlinks。本版不提供加密卷口令流程，也不尝试恢复损坏容器。
因此 APFS 首次验证应打开几种已有文件（文本、照片、视频），并和 Mac 上的已知副本对照。能列目录不等于每个文件都能正确读出。

先检查已有的驱动：

```bash
command -v apfs-fuse
command -v apfsutil
```

如果你自己编译过 apfs-fuse，但可执行文件还在编译目录里，需将你确认来源的可执行文件安装到 `/usr/local/bin/`。不要为了通过探测把未知脚本当作 root 执行。
若没有驱动，可参考上游编译说明：<https://github.com/sgan81/apfs-fuse>。本工具不包含或替换第三方驱动。

指定 APFS 卷的示例（设备路径、卷编号、网络接口必须使用实际值）：

```bash
python3 dist/omarchy-nas.pyz setup --device /dev/sdc2 --name family --apfs-volume 0
```

不要直接照抄示例设备路径。`scan` 输出中的当前路径才是依据。

### 常用命令

安装后会有 `omarchy-nas` 命令：

```bash
omarchy-nas status                  # 实时状态 JSON
omarchy-nas doctor                  # 依赖 / 磁盘 / 网络诊断
sudo omarchy-nas add                # 添加另一个外接数据卷，默认只读
sudo omarchy-nas add --writable     # 明确请求可写共享
sudo omarchy-nas add --writable --claim-root   # ext4/XFS 新卷：卷根目录仅此一层交给共享用户（卷须为空）
sudo omarchy-nas pause family       # 暂停共享并正常卸载；不要在传输中使用
sudo omarchy-nas resume family      # 重新核验并挂载
sudo omarchy-nas remove family      # 取消登记并卸载；不删除卷内文件
sudo omarchy-nas passwd             # 修改本工具的 NAS 密码
sudo omarchy-nas stop               # 停用服务/自动启动并正常卸载，保留配置
sudo omarchy-nas start              # 重新启用
sudo omarchy-nas network            # 路由器/网段或家庭网卡变化后重新登记网络
```

暂停或移除共享时，本工具自己的 Samba 进程会重启，其他共享的连接也会短暂断开。先结束传输。
只有卸载成功后再拔 USB；一块盘若有多个登记卷，先逐个暂停。忙碌导致卸载失败时不会强制/懒卸载，也不会声称可拔出。
重新插入后默认每 20 秒尝试恢复**仍处于启用状态**的已登记卷。主动暂停的卷需要 `resume`，不会自行恢复。
突然断线造成的陈旧挂载会报告“陈旧挂载”错误并给出 `pause` / `resume` 命令，不强行卸载；ext4/XFS 按设备号、APFS 按挂载时的实际设备节点核验，拔插后不会被误判为在线。先 `pause` 再 `resume`，或正常重启后恢复。

默认服务已占 8787 时，不需要再次执行 `serve`，直接打开管理页。临时额外页面可用 `serve --port 8788`。

### 更新

```bash
cd omarchy-nas
git pull
python3 build.py
sudo python3 dist/omarchy-nas.pyz upgrade
```

`upgrade` 安装新版本程序和服务文件并重启运行中的服务，不动登记的卷和 NAS 密码。

### 卸载

1. 运行 `sudo omarchy-nas stop`：停用服务和自动启动，正常卸载所有登记卷。如果它报告有卷仍忙碌，到此为止，不要拔盘。
2. 删除 setup 安装的文件：

   ```bash
   sudo rm -f /etc/systemd/system/omarchy-nas-*.service /etc/systemd/system/omarchy-nas-*.timer
   sudo systemctl daemon-reload
   sudo rm -rf /opt/omarchy-nas /etc/omarchy-nas /var/lib/omarchy-nas /run/omarchy-nas
   sudo rm -f /usr/local/bin/omarchy-nas
   sudo sh -c 'rmdir /srv/omarchy-nas/* /srv/omarchy-nas'   # 只删除空挂载点
   ```

   `rmdir` 只删除空目录，仍在挂载的卷会被拒绝，不会受影响。
3. 删除你为 445 端口加的防火墙规则：`sudo ufw status numbered`，再 `sudo ufw delete <编号>`。

数据盘里的文件不会被动。

### 安装位置

- 程序：`/opt/omarchy-nas/omarchy-nas.pyz`
- 命令：`/usr/local/bin/omarchy-nas`
- 卷登记：`/etc/omarchy-nas/config.json`
- 独立 Samba 配置：`/etc/omarchy-nas/smb.conf`
- 独立 Samba 状态/账户：`/var/lib/omarchy-nas/`
- 挂载根目录：`/srv/omarchy-nas/`
- 状态缓存：`/run/omarchy-nas/status.json`
- systemd 服务：`omarchy-nas-smb.service`、`omarchy-nas-panel.service`、`omarchy-nas-reconcile.service/.timer`
- FUSE 瞬态服务：`omarchy-nas-volume-共享名.service`

### 边界

不修改 `/etc/fstab`、现有 `/etc/samba/smb.conf`、用户 sudoers 或防火墙。Omarchy 默认启用 UFW 并拒绝入站，需要手动只对家庭网段放行 445（SMB），`doctor` 会给出命令。检测到已有 Samba 服务会停止本次设置，不抢占。
Samba 只监听本机回环和登记的家庭网卡（`interfaces = lo <网卡>`，`bind interfaces only = yes`），并且只接受 127.0.0.1 和登记家庭 IPv4 网段的连接，其他来源（包括公网 IPv6）一律拒绝。状态页服务使用 systemd 沙箱（ProtectHome、ProtectSystem=full、PrivateTmp 等），不能访问家目录和 docker.sock。
只在第一次手动登记后自动挂载该卷；不会把新插入的所有设备自动公开。
UUID/PARTUUID、容量、文件系统和可用的硬盘序列号一起检查。克隆造成的重复 UUID 会被拒绝。
空挂载点设为 root 所有的 000 权限，且 Samba 每次新连接都核验真实挂载；外链/符号链接不跟随。
默认只读不代表损坏盘可以随意尝试；读取错误会直接报告，不会自动运行 fsck 或强制恢复。

### 故障查看

```bash
omarchy-nas doctor
omarchy-nas status
systemctl status omarchy-nas-smb omarchy-nas-panel omarchy-nas-reconcile
journalctl -u omarchy-nas-reconcile -n 60 --no-pager
```

- 挂载点被其他程序占用：先在原程序正常卸载。
- Linux 文件系统权限不足：已有权限会保留，程序不会递归 chown/chmod。首版共享的是整个卷根目录，可写卷要求共享用户能写卷根目录。新建 ext4 时用 `mkfs.ext4 -E root_owner=$(id -u):$(id -g)`；已建好的空卷（只有 `lost+found`）可在 `add --writable` 时加 `--claim-root`，只改卷根这一层。已有数据的卷请自行决定授权方式。
- APFS 无法获取卷：检查卷编号、是否加密、是否 Fusion Drive、驱动版本；不要改用可写或 lax 模式绕过错误。
- `testparm` 失败：旧配置保留，不启动新共享。
- 首次 setup 中途失败且已保存 config：使用 `sudo omarchy-nas start` 恢复，不要删数据卷。
- 连接被拒绝：检查输出的主机 IP、所选网络接口、家庭网段和防火墙（`sudo ufw status`）；管理页的 localhost 地址不是另一台机器的文件共享地址。网段或网卡变了用 `sudo omarchy-nas network`。

### 验证

52 项自动测试覆盖磁盘登记与挂载保护、APFS 只读、Samba 配置、systemd 沙箱和状态页。截至 2026-10-29，omarchy-nas 已在一台真实的 Omarchy 电脑上稳定运行两周。

### 源码与复现

`.pyz` 是标准 ZIP，包含 Python 源码、网页、测试和本说明，无需 pip/npm。

```bash
python3 -m unittest discover -s tests -v
python3 build.py
```

拿到的是 `.pyz` 时，可用 `python3 -m zipfile -e omarchy-nas.pyz omarchy-nas-src` 解出源码。
Python 3.11+，目标 Omarchy / Arch Linux + systemd。其他发行版的安装路径未验证。

### 许可证

以 [GNU Affero General Public License v3.0](LICENSE) 发布。Copyright © 2026 Charles Tao。这是社区工具，与 Omarchy 官方无关。
