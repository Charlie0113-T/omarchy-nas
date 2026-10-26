# Omarchy NAS

An [Omarchy](https://github.com/basecamp/omarchy) 4 bar plugin for the machine that runs [omarchy-nas](https://github.com/RoyTao2025/FD/tree/main/services/omarchy-nas): which shares are online, paused or offline, with pause, resume and diagnostics one click away.

在运行 omarchy-nas 的那台 Omarchy 电脑上，把 NAS 的状态放进顶栏。中文说明见[下方](#中文)。

## Requirements

- Omarchy 4: the desktop runs as omarchy-shell, which has the plugin system.
- omarchy-nas set up on the same machine (`omarchy-nas setup`). The plugin shows this machine's NAS only; it doesn't browse shares from other computers.

## Install

```bash
omarchy plugin add https://github.com/RoyTao2025/omarchy_nas.git --enable
```

The icon lands on the right of the bar. Update with `omarchy plugin update roytao2025.omarchy-nas`, remove with `omarchy plugin remove roytao2025.omarchy-nas`.

## What you see

- **The bar icon** has the normal colour while everything is fine. It turns the urgent colour when a share is offline, the service is down, omarchy-nas reports an error, or the file gateway's certificate has 14 days or fewer. It is faded until the first status arrives, and while omarchy-nas isn't installed or set up. Hover for a one-line summary ("NAS · 2/2 online"); right-click to refresh.
- **The panel** lists each share: its state, name, size, file system and mode, or why it is offline.
  - Click a share to copy its `smb://` address; its icon turns into a check.
  - The button at the end of the row pauses or resumes that share.
  - With under 30 days left on the gateway certificate, a row offers to renew it.
  - The footer opens the local status page or runs diagnostics.
- **Keys**: ↑↓ pick a share, Enter copies its address, `p` pauses or resumes it, `r` refreshes, `s` opens the status page, Esc closes.
- **Languages**: Simplified Chinese, Traditional Chinese and English, following Omarchy's language setting.

## Safety

- The plugin only reads status, and never with sudo. It asks the local status page (`http://127.0.0.1:8787/api/status`) and, when that isn't running, runs `omarchy-nas status`.
- Actions that need root (pause, resume, renew) open a terminal that shows the full command, where you type your own password. Nothing runs in the background.
- Only share names omarchy-nas itself accepts (a lowercase letter, then letters, digits, `-` or `_`) ever reach a command line.

## Settings

Settings sit inline on the widget's entry in `~/.config/omarchy/shell.json`, for example:

```json
{ "id": "roytao2025.omarchy-nas", "refreshIntervalSec": 120 }
```

| Key | Default | Range | What it does |
|---|---|---|---|
| `refreshIntervalSec` | 60 | 15–3600 | How often to read the status. Opening the panel always refreshes. |
| `statusPort` | 8787 | 1024–65535 | Port of the local omarchy-nas status page. |

## Development

```bash
omarchy plugin validate .            # Omarchy's own manifest checks
node --test tests/model.test.mjs     # status logic, texts and commands
```

omarchy-shell reloads an installed plugin when one of its files is saved.

## License

AGPL-3.0, like omarchy-nas.

## 中文

**需要**：Omarchy 4，并且同一台电脑上已经用 `omarchy-nas setup` 设置好 NAS。插件只看本机。

**安装**：

```bash
omarchy plugin add https://github.com/RoyTao2025/omarchy_nas.git --enable
```

图标在顶栏右侧。更新：`omarchy plugin update roytao2025.omarchy-nas`；卸载：`omarchy plugin remove roytao2025.omarchy-nas`。

**看到什么**：

- **图标颜色**：一切正常是普通颜色。有共享离线、服务没运行、出错，或网关证书只剩 14 天以内，变成警示色。还没读到状态、没安装或没设置时颜色变淡。
- **悬停和右键**：鼠标停在图标上显示一行摘要，右键立即刷新。
- **面板**：
  - 每个共享一行，写着容量、文件系统、只读还是可读写；离线时写原因。点一行复制 `smb://` 地址。
  - 行尾按钮暂停或继续这个共享。
  - 证书剩不到 30 天时有续期按钮。底部打开状态页、运行诊断。
- **键盘**：↑↓ 选共享，Enter 复制，`p` 暂停/继续，`r` 刷新，`s` 状态页，Esc 关闭。

**安全**：

- 插件只读状态，从不运行 sudo。
- 需要 root 的操作会打开终端显示完整命令，由你自己输入密码。
