# Omarchy NAS 0.3.0

把 USB 硬盘柜接到运行 Omarchy 的电脑，用已有数据卷提供家庭 SMB 共享。
这是独立社区扩展，不依赖未公开的 Omarchy 插件 API。

本版交付：设备扫描、交互设置、只读/可写共享、访问密码、重启/重新插入后恢复已登记卷、暂停/恢复、状态前端，以及 Family Dashboard 的 LAN HTTPS 文件网关。
当前状态：**代码及模拟验证完成，等待 Mac mini Late 2014 + MAIWO + 实际 APFS 卷的现场验收**。
不能把这里的验证结果视为数据盘已挂载成功。

## 开始

与 FD 配对、证书安装及网页上传请先看 [家庭文件接入说明](../../docs/family-nas.md)。从 FD 仓库根目录运行 `python3 services/omarchy-nas/build.py` 生成 `services/omarchy-nas.pyz`。

将 `omarchy-nas.pyz` 放在那台 Omarchy 电脑的 `~/Downloads/`。

```bash
sudo pacman -S --needed samba ntfs-3g && python3 ~/Downloads/omarchy-nas.pyz setup
```

已有 Samba/NTFS 依赖时直接运行：

```bash
python3 ~/Downloads/omarchy-nas.pyz setup
```

设置向导会申请 sudo 权限，依次让你选择家庭网络接口、外接数据卷、共享名；显示容量、卷标识和读写模式后确认，再单独设置 NAS 密码。APFS 需要明确输入卷编号。
默认共享为**只读**。仅在支持的文件系统上，明确加 `--writable` 才启用写入。

完成后：

- 本机管理页：<http://127.0.0.1:8787>
- 终端打印真实 `smb://主机IP/共享名`，以实际输出为准。
- Mac：Finder → 前往 → 连接服务器。
- iPad：文件 → ⋯ → 连接服务器 → 注册用户。
- Windows：文件资源管理器地址栏输入 `\\主机IP\共享名`。
- 使用向导显示的 Linux 用户名和**刚设置的 NAS 密码**。NAS 密码存放在本插件自己的 Samba 账户库，不改 Linux 登录密码。

管理页提供状态和操作命令。复制命令到主机终端后执行；网页本身不持有 root 权限，不提供远程磁盘管理接口。
本版共享只允许所选家庭私有 IPv4 网段，不自动调整路由器或防火墙。若防火墙拦截，只为这个网段允许 TCP 445。

## 先查看，不安装

```bash
python3 ~/Downloads/omarchy-nas.pyz scan
python3 ~/Downloads/omarchy-nas.pyz doctor
python3 ~/Downloads/omarchy-nas.pyz serve --demo
```

`scan` / `doctor` 不改磁盘。`serve --demo` 是明确标注的虚构数据预览，不会挂载、建立共享或改变主机。
不用 `--demo` 时读取真实本机状态，须用普通用户运行。

## 文件系统范围

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
程序不处理格式化、分区、修复、自动解密、Fusion Drive 拼接、Time Machine、NFS 或公网穿透。Web 上传仅通过单独配对的家庭 HTTPS 网关向可写卷提供；APFS 不提供写入。
数据盘全是 APFS 时，可以在 Mac 上缩小 APFS 容器，再手动新建一个 ext4 上传分区，步骤与风险见 [家庭文件接入说明 · 第 0 步](../../docs/family-nas.md#0-数据盘全是-apfs-时先准备一个可写分区)。要在 `setup` 之前完成：已登记的卷按容量核对身份。

## Charles 的 APFS 硬盘柜

你之前的挂载错误尚未有真机成功结果。本版增加了：

1. APFS GPT 分区类型识别（lsblk 尚未显示 FSTYPE 时也能提示）；有 UUID 用 UUID，没有则用 PARTUUID。
2. 首次接入显示 `apfsutil` 容器信息（该工具已安装时），明确选择 APFS 卷编号；不默选“第一个就是数据卷”。
3. 指定普通用户 UID/GID、allow_other、只读及独立 FUSE subtype，核验挂载来源后才共享。
4. APFS/NTFS 的 FUSE 进程由独立的 systemd transient service 持有，避免定时检查退出时顺手杀掉挂载进程。
5. 错误会保留在终端、systemd 日志以及状态页中；没有挂上时不会标成成功。

apfs-fuse 上游明确说明：它是只读驱动，未支持所有压缩方法，也不支持 firmlinks。本版不提供加密卷口令流程，也不尝试恢复损坏容器。
因此 APFS 首次验证应打开几种已有文件（文本、照片、视频），并和 Mac 上的已知副本对照。能列目录不等于每个文件都能正确读出。

先检查你已有的驱动：

```bash
command -v apfs-fuse
command -v apfsutil
```

若 Pi 已经编译了 apfs-fuse，但它只在编译目录，需将你确认来源的可执行文件安装到 `/usr/local/bin/`。不要为了通过探测把未知脚本当作 root 执行。
若没有驱动，可参考上游编译说明：<https://github.com/sgan81/apfs-fuse>。本安装包不含或替换第三方驱动。

指定 APFS 卷的示例（设备路径、卷编号、网络接口必须使用实际值）：

```bash
python3 ~/Downloads/omarchy-nas.pyz setup --device /dev/sdc2 --name family --apfs-volume 0
```

不要直接照抄示例设备路径。`scan` 输出中的当前路径才是依据。

## 常用命令

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
sudo omarchy-nas passwd             # 修改本插件的 NAS 密码
sudo omarchy-nas stop               # 停用服务/自动启动并正常卸载，保留配置
sudo omarchy-nas start              # 重新启用
sudo omarchy-nas network            # 路由器/网段或家庭网卡变化后重新登记网络（之后需 gateway-init --replace）
sudo omarchy-nas gateway-renew      # 续期 FD 网关服务器证书，凭据和配对不变
sudo python3 ~/Downloads/omarchy-nas.pyz upgrade   # 安装新版本程序并重启服务，不动配对和证书
```

暂停或移除共享时，本插件自己的 Samba 进程会重启，其他共享的连接也会短暂断开。先结束传输。
只有卸载成功后再拔 USB；一块盘若有多个登记卷，先逐个暂停。忙碌导致卸载失败时不会强制/懒卸载，也不会声称可拔出。
重新插入后默认每 20 秒尝试恢复**仍处于启用状态**的已登记卷。主动暂停的卷需要 `resume`，不会自行恢复。
突然断线造成的陈旧挂载会报告“陈旧挂载”错误并给出 `pause` / `resume` 命令，不强行卸载；ext4/XFS 按设备号、APFS 按挂载时的实际设备节点核验，拔插后不会被误判为在线。先 `pause` 再 `resume`，或正常重启后恢复。

默认服务已占 8787 时，不需要再次执行 `serve`，直接打开管理页。临时额外页面可用 `serve --port 8788`。

## 安装位置与边界

- 程序：`/opt/omarchy-nas/omarchy-nas.pyz`
- 命令：`/usr/local/bin/omarchy-nas`
- 卷登记：`/etc/omarchy-nas/config.json`
- 独立 Samba 配置：`/etc/omarchy-nas/smb.conf`
- 独立 Samba 状态/账户：`/var/lib/omarchy-nas/`
- 挂载根目录：`/srv/omarchy-nas/`
- 状态缓存：`/run/omarchy-nas/status.json`
- systemd 服务：`omarchy-nas-smb.service`、`omarchy-nas-panel.service`、`omarchy-nas-reconcile.service/.timer`
- FUSE 瞬态服务：`omarchy-nas-volume-共享名.service`
- FD 网关：`omarchy-nas-gateway.service`，配置 `/etc/omarchy-nas/gateway.json`，配对后启用家庭 HTTPS 端口 8789；`stop` / `start` 一并管理该服务。

不修改 `/etc/fstab`、现有 `/etc/samba/smb.conf`、用户 sudoers 或防火墙。Omarchy 默认启用 UFW 并拒绝入站，需要手动只对家庭网段放行 445（SMB）和 8789（FD 网关），`doctor` 和 `gateway-init` 会给出命令。检测到已有 Samba 服务会停止本次设置，不抢占。
Samba 只监听本机回环和登记的家庭网卡（`interfaces = lo <网卡>`，`bind interfaces only = yes`），并且只接受 127.0.0.1 和登记家庭 IPv4 网段的连接，其他来源（包括公网 IPv6）一律拒绝。网关与状态页服务使用 systemd 沙箱（ProtectHome、ProtectSystem=full、PrivateTmp 等），不能访问家目录和 docker.sock。
只在第一次手动登记后自动挂载该卷；不会把新插入的所有设备自动公开。
UUID/PARTUUID、容量、文件系统和可用的硬盘序列号一起检查。克隆造成的重复 UUID 会被拒绝。
空挂载点设为 root 所有的 000 权限，且 Samba 每次新连接都核验真实挂载；外链/符号链接不跟随。
默认只读不代表损坏盘可以随意尝试；读取错误会直接报告，不会自动运行 fsck 或强制恢复。

## 故障查看

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

## 验证记录 · 2026-10-04

云端已完成代码检查（FD 124 项、Python 109 项自动测试，构建与 lint，三轮审查）和**模拟**传输检查（真实网关代码 + 临时目录；无头 Chromium + 真实 FD 页面，网络与证书信任为模拟）。真机验收（Mac mini、硬盘柜、家庭 Wi-Fi、iPhone/Mac 浏览器）尚未开始。三类结果与待验收清单见 `VERIFICATION.md`。

第一次真机验收顺序：`doctor` → `setup --writable`（ext4 上传盘）→ Mac/iPad 登录 SMB 读写测试文件 → `gateway-init` → 设备信任证书、FD 导入配对 → 上传/下载比对 → pause / resume → 重启后检查。
只有上述成功后，才把相应硬件/文件系统标为实测通过。APFS 不包含写入验收。

## 源码与复现

`.pyz` 是标准 ZIP，包含 Python 源码、网页、测试和本说明，无需 pip/npm。

```bash
python3 -m zipfile -e omarchy-nas.pyz omarchy-nas-src
cd omarchy-nas-src
python3 -m unittest discover -s tests -v
python3 build.py
```

Python 3.11+，目标 Omarchy / Arch Linux + systemd。其他发行版的安装路径未验证。

参考实现依据：

- APFS 驱动与约束：<https://github.com/sgan81/apfs-fuse>
- Samba 配置：<https://www.samba.org/samba/docs/current/man-html/smb.conf.5.html>
- Samba 密码工具：<https://www.samba.org/samba/docs/current/man-html/smbpasswd.8.html>
- lsblk：<https://man.archlinux.org/man/lsblk.8.en>
- mount：<https://man.archlinux.org/man/mount.8.en>
