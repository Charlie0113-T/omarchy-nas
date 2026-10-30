> **Note:** I'm still a student, so I may not be able to respond to issues or ship updates right away. I'll keep maintaining this project in my spare time as best I can.

<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/omarchy-logo-dark.svg">
  <img src="assets/omarchy-logo.svg" alt="Omarchy" width="360">
</picture>

# Omarchy NAS

**Share the USB drives on your Omarchy machine, and see them in the bar: online, paused or offline at a glance**

SMB shares from the disks you already have · Omarchy 4 bar plugin · Never runs `sudo` in the background

![omarchy-nas](https://img.shields.io/badge/omarchy--nas-0.3.0-2ea44f)
![plugin](https://img.shields.io/badge/plugin-0.1.0-2ea44f)
![platform](https://img.shields.io/badge/platform-Omarchy%204-1793D1?logo=archlinux&logoColor=white)
![built with](https://img.shields.io/badge/built%20with-Python%20%2F%20QML-41CD52?logo=qt&logoColor=white)
![license](https://img.shields.io/badge/license-AGPL--3.0-blue)

**English** · [简体中文](#中文)

</div>

---

Two parts in one repository, for an [Omarchy](https://github.com/omacom/omarchy) 4 computer with a USB drive enclosure:

- **omarchy-nas**, in [`service/`](service/README.md), shares the data volumes already on your disks with your home network over SMB: read-only unless you ask for writes, behind a NAS password, with a local status page.
- **The bar plugin**, at the root of this repository, shows which shares are online, paused or offline, and puts pause, resume and diagnostics one click away.

## At a glance

| Part | What it does |
| --- | --- |
| 🗄️ **omarchy-nas** | Shares existing volumes (APFS, ext4, XFS, exFAT, NTFS) over SMB, brings them back after a reboot or re-plug, and never formats a disk. |
| 💾 **Bar icon** | Colour shows the NAS state. Hover for a one-line summary ("NAS · 2/2 online"); right-click to refresh. |
| 📋 **Panel** | One row per share: state, name, size, file system and mode, or why it is offline. |
| ⏯️ **Actions** | Pause or resume a share, open the status page, run diagnostics. |
| ⌨️ **Keys** | Drive the whole panel from the keyboard. |
| 🌐 **Languages** | 简体中文, 繁體中文 and English, following Omarchy's language setting. |

## Requirements

- Omarchy 4: the desktop runs as omarchy-shell, which has the plugin system.
- For omarchy-nas: Python 3.11 or later, `samba`, and `ntfs-3g` for NTFS or `apfs-fuse` for APFS volumes.
- For the bar plugin: omarchy-nas set up on the same machine, plus `curl` and `wl-copy`, which come with Omarchy.

> [!NOTE]
> The plugin shows this machine's NAS only. It doesn't browse shares from other computers.

## 📦 Install

### 1. omarchy-nas

```sh
sudo pacman -S --needed samba ntfs-3g
git clone https://github.com/Charlie0113-T/omarchy-nas.git
cd omarchy-nas/service
python3 build.py
python3 dist/omarchy-nas.pyz setup
```

Setup asks for sudo, then walks you through the home network, the disk and a NAS password; shares are read-only unless you add `--writable`. The full guide (file systems, APFS, every command, firewall, removal) is in [service/README.md](service/README.md).

### 2. The bar plugin

```sh
omarchy plugin add https://github.com/Charlie0113-T/omarchy-nas.git --enable
```

The icon lands on the right of the bar.

### Update and remove

```sh
omarchy plugin update io.github.charlie0113-t.omarchy-nas
omarchy plugin remove io.github.charlie0113-t.omarchy-nas
```

For omarchy-nas itself, see [Update](service/README.md#update) and [Remove](service/README.md#remove) in its guide.

## 🧩 What you see

### The bar icon

| Icon | Meaning |
| --- | --- |
| Normal colour | Everything is fine. |
| Urgent colour | A share is offline, the service is down, or omarchy-nas reports an error. |
| Faded | No status has arrived yet, or omarchy-nas isn't installed or set up. |

### The panel

- Click a share to copy its `smb://` address; its icon turns into a check.
- The button at the end of the row pauses or resumes that share.
- The footer opens the local status page or runs diagnostics.

### Keys

| Key | Action |
| :---: | --- |
| `↑` `↓` | Pick a share |
| `Enter` | Copy its `smb://` address |
| `p` | Pause or resume it |
| `r` | Refresh |
| `s` | Open the status page |
| `Esc` | Close the panel |

## 🔒 Safety

- The plugin only reads status, and never with sudo. It asks the local status page (`http://127.0.0.1:8787/api/status`) and, when that isn't running, runs `omarchy-nas status`.
- Only share names omarchy-nas itself accepts (a lowercase letter, then letters, digits, `-` or `_`) ever reach a command line.
- omarchy-nas doesn't touch `/etc/fstab`, an existing Samba setup, sudoers or the firewall; what it does change is listed under [Boundaries](service/README.md#boundaries).

> [!IMPORTANT]
> Actions that need root (pause, resume) open a terminal that shows the full command, where you type your own password. **Nothing runs in the background.**

## ⚙️ Settings

Settings sit inline on the widget's entry in `~/.config/omarchy/shell.json`, for example:

```json
{ "id": "io.github.charlie0113-t.omarchy-nas", "refreshIntervalSec": 120 }
```

| Key | Default | Range | What it does |
| --- | :---: | :---: | --- |
| `refreshIntervalSec` | 60 | 15–3600 | How often to read the status. Opening the panel always refreshes. |
| `statusPort` | 8787 | 1024–65535 | Port of the local omarchy-nas status page. |

## 🛠️ Development

```text
omarchy-nas/
├── manifest.json        # plugin id, entry point and settings schema
├── Widget.qml           # bar icon and panel
├── Status.qml           # reads the status page, or falls back to omarchy-nas status
├── Model.js             # status logic, texts and commands (no QML, testable in Node)
├── tests/
│   └── model.test.mjs
└── service/             # omarchy-nas itself: Python, no third-party packages
    ├── build.py         # builds dist/omarchy-nas.pyz
    ├── nas/             # the CLI, disk and Samba logic, and the status page
    └── tests/
```

```sh
omarchy plugin validate .                                  # Omarchy's own manifest checks
node --test tests/model.test.mjs                           # the plugin's status logic, texts and commands
(cd service && python3 -m unittest discover -s tests)      # omarchy-nas
```

> [!TIP]
> omarchy-shell reloads an installed plugin when one of its files is saved.

## License

Released under the [GNU Affero General Public License v3.0](LICENSE). Copyright © 2026 Charles Tao.

The Omarchy logo comes from [omacom/omarchy](https://github.com/omacom/omarchy) under the [MIT license](assets/LICENSE). The status page is set in Geist and Geist Mono, © 2023 Vercel in collaboration with basement.studio, under the [SIL Open Font License 1.1](service/nas/assets/Geist-LICENSE.txt). This is a community tool and is not affiliated with Omarchy.

---

## 中文

> **说明：** 我目前还是学生，可能没法及时处理问题或更新，但会在空闲时间尽力维护这个项目。

这个仓库有两部分，给接了 USB 硬盘柜的 Omarchy 电脑用：

- **omarchy-nas**（在 [`service/`](service/README.md#中文)）用硬盘上已有的数据卷提供家庭 SMB 共享：默认只读，需要 NAS 密码，带一个本机状态页。
- **顶栏插件**（仓库根目录）把共享的状态放进顶栏，暂停、继续和诊断一点就到。

### 需要

Omarchy 4。omarchy-nas 需要 Python 3.11 或更新、`samba`，NTFS 卷需要 `ntfs-3g`，APFS 卷需要 `apfs-fuse`。顶栏插件需要同一台电脑上已经设置好 omarchy-nas，插件只看本机；用到的 `curl` 和 `wl-copy` 是 Omarchy 自带的。

### 📦 安装

先装 omarchy-nas：

```sh
sudo pacman -S --needed samba ntfs-3g
git clone https://github.com/Charlie0113-T/omarchy-nas.git
cd omarchy-nas/service
python3 build.py
python3 dist/omarchy-nas.pyz setup
```

设置向导会申请 sudo，依次选择家庭网络、磁盘和 NAS 密码；默认只读，加 `--writable` 才可写。文件系统、APFS、全部命令、防火墙和卸载见 [service/README.md](service/README.md#中文)。

再装顶栏插件：

```sh
omarchy plugin add https://github.com/Charlie0113-T/omarchy-nas.git --enable
```

图标在顶栏右侧。更新：`omarchy plugin update io.github.charlie0113-t.omarchy-nas`；卸载：`omarchy plugin remove io.github.charlie0113-t.omarchy-nas`。omarchy-nas 本身的更新和卸载见它的说明。

### 🧩 看到什么

- **图标颜色**：一切正常是普通颜色。有共享离线、服务没运行或出错，变成警示色。还没读到状态、没安装或没设置时颜色变淡。
- **悬停和右键**：鼠标停在图标上显示一行摘要，右键立即刷新。
- **面板**：
  - 每个共享一行，写着容量、文件系统、只读还是可读写；离线时写原因。点一行复制 `smb://` 地址。
  - 行尾按钮暂停或继续这个共享。
  - 底部打开状态页、运行诊断。

| 按键 | 作用 |
| :---: | --- |
| `↑` `↓` | 选共享 |
| `Enter` | 复制 `smb://` 地址 |
| `p` | 暂停/继续 |
| `r` | 刷新 |
| `s` | 打开状态页 |
| `Esc` | 关闭 |

### 🔒 安全

- 插件读状态从不用 sudo。
- omarchy-nas 不改 `/etc/fstab`、已有的 Samba 配置、sudoers 或防火墙；它会改动的地方见 [边界](service/README.md#边界)。

> [!IMPORTANT]
> 需要 root 的操作会打开终端显示完整命令，由你自己输入密码。
