from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import os
from pathlib import Path
import pwd
import shlex
import shutil
import socket
import sys
import zipfile

from . import core as c


def root():
    if os.geteuid() != 0:
        raise c.NasError("此操作需要 sudo：sudo omarchy-nas " + shlex.join(sys.argv[1:]))


def regular_user(name=None):
    name = name or os.environ.get("SUDO_USER") or getpass.getuser()
    try:
        user = pwd.getpwnam(name)
    except KeyError as e:
        raise c.NasError("Linux 用户不存在") from e
    if user.pw_uid < 1000 or user.pw_name == "nobody":
        raise c.NasError("请由普通桌面用户运行 setup；不要使用 root 作为共享用户。")
    return user


def table(devices):
    for index, d in enumerate(devices, 1):
        state = "可接入" if d["eligible"] else d["reason"]
        print(f"{index:>2}. {c.clean(d['path']):<20} {c.human_size(d['size']):>11}  {d['fs'] or '—':<7}  {d['label'] or d['model']}")
        print(f"    {state}")


def choose(items, prompt, formatter):
    if not items:
        raise c.NasError("没有可选项。运行 doctor 查看原因。")
    for i, item in enumerate(items, 1):
        print(f"  {i}. {formatter(item)}")
    answer = input(prompt).strip()
    if not answer.isdigit() or not 1 <= int(answer) <= len(items):
        raise c.NasError("没有选择有效编号，未执行变更。")
    return items[int(answer) - 1]


def confirm(message):
    if input(message + " 输入 yes 继续：").strip() != "yes":
        raise c.NasError("已取消。")


def dependencies(fs=None):
    required = ["lsblk", "findmnt", "ip", "mount", "umount", "systemctl", "systemd-run", "testparm", "smbd", "smbpasswd", "runuser"]
    if fs == "apfs":
        required.append("apfs-fuse")
    if fs == "ntfs":
        required.append("ntfs-3g")
    missing = [x for x in required if not shutil.which(x, path=c.SAFE_PATH)]
    if missing:
        raise c.NasError("缺少：" + ", ".join(missing) + "\nOmarchy 常规依赖：sudo pacman -S --needed samba ntfs-3g\nAPFS 另需 apfs-fuse；不会自动执行未知驱动安装脚本。")


def service_active(name):
    return c.run(["systemctl", "is-active", "--quiet", name], check=False).returncode == 0


def check_conflict():
    for service in ("smb", "smbd", "samba", "samba-ad-dc"):
        if service_active(service):
            raise c.NasError(f"已有 {service}.service 在运行。未停止或覆盖它；需要先决定复用还是迁移。")
    if not service_active(c.SERVICE):
        try:
            with socket.socket() as sock:
                sock.bind(("0.0.0.0", 445))
        except OSError as e:
            raise c.NasError("445 端口已占用或不可绑定，未启动新共享。") from e


def enroll(device, args, cfg):
    if not device["eligible"]:
        raise c.NasError(device["reason"])
    name = args.name or input("共享名（小写英文，如 family）：").strip()
    c.valid_name(name)
    mode = "rw" if args.writable else "ro"
    if mode == "rw" and device["fs"] == "apfs":
        raise c.NasError("APFS 不能设为可写。")
    if mode == "rw" and device["readonly"]:
        raise c.NasError("设备报告只读，不能设为可写。")
    claim = getattr(args, "claim_root", False)
    if claim and (mode != "rw" or device["fs"] not in {"ext4", "xfs"}):
        raise c.NasError("--claim-root 只用于加了 --writable 的 ext4 / XFS 卷。")
    dependencies(device["fs"])
    volume = args.apfs_volume
    if device["fs"] == "apfs" and volume is None:
        if shutil.which("apfsutil", path=c.SAFE_PATH):
            print(c.clean("APFS 容器信息："))
            # apfsutil prints volume indices; preserve line breaks but not terminal controls.
            out = c.run(["apfsutil", device["path"]], check=False, timeout=20)
            print("\n".join(c.clean(line) for line in (out.stdout + out.stderr).splitlines())[:7000])
        print("APFS 容器可能包含多个卷。首版一次共享其中一个未加密卷。")
        value = input("请输入要接入的卷编号（从 0 开始）：").strip()
        if not value.isdigit():
            raise c.NasError("需要明确的 APFS 卷编号。")
        volume = int(value)
    id_type = "uuid" if device["uuid"] else "partuuid"
    share = {"name": name, "id_type": id_type, "id": device[id_type],
             "fs": device["fs"], "size": device["size"], "serial": device["serial"],
             "mode": mode, "volume": volume or 0, "enabled": True}
    candidate = {**cfg, "shares": cfg["shares"] + [share]}
    c.validate_config(candidate)
    print(f"\n接入 {device['path']} · {device['label'] or device['model']} · {c.human_size(device['size'])}")
    print(f"共享 {name} · {'可读写' if mode == 'rw' else '只读'} · {id_type.upper()}={device[id_type]}")
    print(f"访问用户 {cfg['user']} · 允许家庭网段 {cfg['network']}\n不会格式化或递归修改已有文件权限。")
    if claim:
        print(f"--claim-root：若 {cfg['user']} 不能写入卷根目录且其中只有 lost+found，只把卷根目录本身改为 {cfg['user']} 所有、权限 0700（不递归）。")
    confirm("登记这个卷并启用共享？")
    return candidate, share


def selected_device(args):
    devices = c.inventory()
    if args.device:
        matches = [d for d in devices if d["path"] == args.device]
        if len(matches) != 1:
            raise c.NasError("设备不存在或不唯一；请重新 scan。")
        return matches[0]
    candidates = [d for d in devices if d["eligible"] and not d["mounts"]]
    if not candidates:
        table(devices)
        raise c.NasError("没有可接入的新卷。以上列出了具体原因。")
    return choose(candidates, "选择磁盘卷编号：", lambda d: f"{d['path']} · {c.human_size(d['size'])} · {d['fs']} · {d['label'] or d['model']}")


def copy_app():
    source = Path(sys.argv[0]).resolve()
    if source == c.P.app:
        return
    if not source.is_file() or not zipfile.is_zipfile(source):
        raise c.NasError("安装请使用打包的 omarchy-nas.pyz，而不是源码目录。")
    # Source is an explicitly invoked user-provided program. Copy to a root-owned immutable-to-user location.
    candidate = c.P.app.with_suffix(".next")
    with source.open("rb") as src, candidate.open("wb") as dst:
        shutil.copyfileobj(src, dst)
    os.chmod(candidate, 0o644)
    os.chown(candidate, 0, 0)
    os.replace(candidate, c.P.app)
    launcher = Path("/usr/local/bin/omarchy-nas")
    c.secure_dir(launcher.parent)
    if launcher.exists() and "# Omarchy NAS launcher" not in launcher.read_text(errors="replace"):
        raise c.NasError("已有同名 omarchy-nas 命令，拒绝覆盖。")
    c.atomic(launcher, f'#!/bin/sh\n# Omarchy NAS launcher\nexec {c.PYTHON} {c.P.app} "$@"\n', mode=0o755)


def set_password(cfg):
    first = getpass.getpass("设置 NAS 密码（至少 8 位，不会更改 Linux 登录密码）：")
    second = getpass.getpass("再次输入 NAS 密码：")
    if first != second or len(first) < 8 or "\n" in first or "\r" in first:
        raise c.NasError("两次密码不一致或不符合长度要求。")
    c.run(["smbpasswd", "-c", str(c.P.smb), "-a", "-s", cfg["user"]], input=first + "\n" + first + "\n")


def write_units(cfg):
    units = c.unit_files(cfg)
    if (c.P.etc / "gateway.json").exists():
        from .gateway_setup import SERVICE, unit
        units[SERVICE] = unit(cfg)
    for name, content in units.items():
        existing = c.P.units / name
        if existing.exists() and "omarchy-nas" not in existing.read_text():
            raise c.NasError(f"系统已有同名服务：{name}")
        c.atomic(existing, content)
    c.run(["systemctl", "daemon-reload"])


def pairing_hint(share):
    # gateway.json binds each paired name to the volume identity seen at gateway-init time.
    path = c.P.etc / "gateway.json"
    if not path.exists():
        return
    try:
        paired = json.loads(path.read_text()).get("shares", [])
    except (OSError, ValueError, AttributeError):
        paired = []
    name = share["name"]
    entry = next((s for s in paired if s == name or isinstance(s, dict) and s.get("name") == name), None)
    if entry is None or isinstance(entry, dict) and any(entry.get(k) != share[k] for k in ("id_type", "id", "fs")):
        print((f"注意：FD 文件网关的配对中还没有 {name}" if entry is None else
               f"注意：FD 文件网关配对时的 {name} 是另一个卷（例如已重新格式化或更换磁盘）") +
              "，FD 暂时无法使用它。请在 FD 设置页复制 gateway-init 命令，加 --replace 重新运行，再在各设备导入新的 pairing.json。")


def setup(args):
    if os.geteuid() != 0:
        # Elevate only setup; the panel always runs as the desktop user.
        os.execvp("sudo", ["sudo", c.PYTHON, str(Path(sys.argv[0]).resolve()), *sys.argv[1:]])
    root()
    dependencies()
    user = regular_user(args.user)
    if c.P.config.exists():
        raise c.NasError("已完成初始化。添加磁盘用 sudo omarchy-nas add；重新启动用 sudo omarchy-nas start。")
    check_conflict()
    if not Path("/run/systemd/system").exists():
        raise c.NasError("需要正在运行 systemd 的 Linux 主机；此环境不能安装服务。")
    interfaces = c.network_interfaces()
    if args.interface:
        interfaces = [i for i in interfaces if i["name"] == args.interface]
    iface = choose(interfaces, "选择家庭网络接口编号：", lambda i: f"{i['name']} · {i['ip']} · {i['network']}")
    cfg = {"schema": 1, "version": c.VERSION, "user": user.pw_name, "uid": user.pw_uid,
           "gid": user.pw_gid, "interface": iface["name"], "network": iface["network"], "shares": []}
    cfg, share = enroll(selected_device(args), args, cfg)
    c.prepare_paths()
    copy_app()
    c.write_smb(cfg)
    # Authenticate before enrolling or starting any network daemon.
    set_password(cfg)
    with c.locked():
        c.ensure_mounted(share, cfg, c.inventory(), claim=args.claim_root)
        c.save_config(cfg)
    write_units(cfg)
    c.reconcile()
    c.run(["systemctl", "enable", "--now", c.SERVICE, "omarchy-nas-reconcile.timer", "omarchy-nas-panel.service"], timeout=60)
    print(f"\nNAS 已启用：smb://{iface['ip']}/{share['name']}\n账号：{cfg['user']}\n本机管理页：http://127.0.0.1:8787\n查看状态：omarchy-nas status")


def add(args):
    root()
    cfg = c.load_config()
    candidate, share = enroll(selected_device(args), args, cfg)
    with c.locked():
        # Re-read to reject a concurrent edit made while waiting for interactive input.
        if c.load_config() != cfg:
            raise c.NasError("配置刚刚发生变化，请重试。")
        mounted = False
        try:
            c.ensure_mounted(share, candidate, c.inventory(), claim=args.claim_root)
            mounted = True
            c.write_smb(candidate)
            c.save_config(candidate)
        except Exception:
            c.write_smb(cfg)
            if mounted:
                with contextlib.suppress(c.NasError):
                    c.run(["umount", "--", str(c.P.mounts / share["name"])])
            raise
    if service_active(c.SERVICE):
        c.run(["systemctl", "reload", c.SERVICE])
    c.reconcile()
    print(f"已登记共享：{share['name']}")
    pairing_hint(share)


def disable_share(name, *, forget=False):
    root()
    with c.locked():
        cfg = c.load_config()
        shares = [s for s in cfg["shares"] if s["name"] == name]
        if not shares:
            raise c.NasError("共享不存在")
        # Disable first so the timer cannot remount it, even if unmount fails.
        shares[0]["enabled"] = False
        c.save_config(cfg)
        c.write_smb(cfg)
        if service_active(c.SERVICE):
            # Restart closes existing SMB file handles before attempting a normal unmount.
            c.run(["systemctl", "restart", c.SERVICE])
        target = c.P.mounts / c.valid_name(name)
        if c.mount_info(target):
            c.run(["umount", "--", str(target)], timeout=45)
        if forget:
            cfg["shares"] = [s for s in cfg["shares"] if s["name"] != name]
            c.save_config(cfg)
        print(f"{name} 已{'移除登记' if forget else '暂停并卸载'}。如同一硬盘有其他共享，请逐一暂停后再拔线。")


def enable_share(name):
    root()
    with c.locked():
        cfg = c.load_config()
        shares = [s for s in cfg["shares"] if s["name"] == name]
        if not shares:
            raise c.NasError("共享不存在")
        c.ensure_mounted(shares[0], cfg, c.inventory())
        shares[0]["enabled"] = True
        c.write_smb(cfg)
        c.save_config(cfg)
    if service_active(c.SERVICE):
        c.run(["systemctl", "reload", c.SERVICE])
    c.reconcile()
    print(f"已恢复：{name}")
    pairing_hint(shares[0])


def stop():
    root()
    if (c.P.etc / "gateway.json").exists():
        c.run(["systemctl", "disable", "--now", "omarchy-nas-gateway.service"])
    c.run(["systemctl", "disable", "--now", "omarchy-nas-reconcile.timer", c.SERVICE, "omarchy-nas-panel.service"])
    # Also wait for an already-triggered reconcile job, not just its timer.
    c.run(["systemctl", "stop", "omarchy-nas-reconcile.service"], timeout=60)
    failures = []
    with c.locked():
        for s in c.load_config()["shares"]:
            target = c.P.mounts / s["name"]
            try:
                if c.mount_info(target):
                    c.run(["umount", "--", str(target)], timeout=45)
            except c.NasError as e:
                failures.append(str(e))
    if failures:
        raise c.NasError("服务已停止，但有卷仍忙碌，请勿拔盘：\n" + "\n".join(failures))
    print("NAS 服务和自动启动已关闭，登记卷已正常卸载。配置保留，可用 start 恢复。")


def start():
    root()
    cfg = c.load_config()
    check_conflict()
    c.prepare_paths()
    c.write_smb(cfg)
    write_units(cfg)
    states = c.reconcile()
    c.run(["systemctl", "enable", "--now", c.SERVICE, "omarchy-nas-reconcile.timer", "omarchy-nas-panel.service"], timeout=60)
    if (c.P.etc / "gateway.json").exists():
        c.run(["systemctl", "enable", "--now", "omarchy-nas-gateway.service"])
    print(json.dumps(states, ensure_ascii=False, indent=2))


def renetwork(args):
    root()
    interfaces = c.network_interfaces()
    if args.interface:
        interfaces = [i for i in interfaces if i["name"] == args.interface]
    iface = choose(interfaces, "选择家庭网络接口编号：", lambda i: f"{i['name']} · {i['ip']} · {i['network']}")
    with c.locked():
        cfg = c.load_config()
        # Only the interface and subnet change; shares, modes and the APFS read-only rule are revalidated.
        candidate = c.validate_config({**cfg, "interface": iface["name"], "network": iface["network"]})
        c.write_smb(candidate)
        c.save_config(candidate)
    # Samba binds listening addresses at start; a reload would keep the old interface.
    c.run(["systemctl", "try-restart", c.SERVICE], timeout=60)
    from .gateway_setup import firewall_note
    print(f"家庭网络：{cfg['interface']} · {cfg['network']} → {iface['name']} · {iface['network']}。共享与读写模式未改变。")
    print(firewall_note(iface["network"], iface["ip"]))
    if (c.P.etc / "gateway.json").exists():
        print("FD 文件网关仍使用旧地址：请在 FD 设置页复制命令，加 --replace 重新运行 gateway-init，再在设备导入新的 pairing.json。")


def upgrade():
    root()
    cfg = c.load_config()
    c.prepare_paths()
    copy_app()
    # Never touches gateway.json, pairing.json or TLS files; only program, smb.conf and units.
    c.write_smb(cfg)
    write_units(cfg)
    c.run(["systemctl", "try-restart", c.SERVICE, "omarchy-nas-panel.service"], timeout=60)
    path = c.P.etc / "gateway.json"
    if path.exists() and service_active("omarchy-nas-gateway.service"):
        from .gateway_setup import wait_ready
        gateway = json.loads(path.read_text())
        c.run(["systemctl", "restart", "omarchy-nas-gateway.service"])
        wait_ready(gateway["bind"], gateway["port"])
    print(f"已升级到 {c.VERSION}；正在运行的服务已重启。配对、凭据与证书未改变。")


def doctor():
    print(f"Omarchy NAS {c.VERSION} · 只读诊断\n")
    for name in ("python3", "lsblk", "findmnt", "ip", "smbd", "testparm", "smbpasswd", "ntfs-3g", "apfs-fuse", "apfsutil"):
        print(f"{name:<13} {shutil.which(name, path=c.SAFE_PATH) or '未安装'}")
    print("\n磁盘：")
    table(c.inventory())
    print("\n家庭网络：")
    interfaces = c.network_interfaces()
    for iface in interfaces:
        print(f"{iface['name']} · {iface['ip']} · {iface['network']}")
    network, address = "<家庭网段>", "<NAS IP>"
    with contextlib.suppress(c.NasError, OSError, ValueError, KeyError):
        cfg = c.load_config()
        found = [i["ip"] for i in interfaces if i["name"] == cfg["interface"]]
        network, address = cfg["network"], found[0] if found else address
    ufw = {True: "已启用（Omarchy 默认拒绝入站）", False: "未启用", None: "未检测到 /etc/ufw/ufw.conf"}[c.ufw_enabled()]
    print("\n若 lsblk 未显示 UUID，可用 sudo omarchy-nas scan 再检查。\n管理页只监听本机；共享通过所选家庭接口的 SMB3 / TCP 445 访问，FD 文件网关使用 HTTPS / TCP 8789。")
    print(f"UFW：{ufw}\n防火墙只向家庭网段放行 8789 与 445（本程序不会修改防火墙）：")
    print("\n".join("  " + line for line in c.firewall_commands(network, address)))


def parser():
    p = argparse.ArgumentParser(description="Omarchy NAS — 接入现有 USB 数据盘，提供带密码的家庭 SMB 共享。")
    p.add_argument("--version", action="version", version=c.VERSION)
    commands = p.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan", help="识别磁盘；不会挂载或修改")
    scan.add_argument("--json", action="store_true")
    commands.add_parser("doctor", help="检查依赖和设备")
    for name in ("setup", "add"):
        sub = commands.add_parser(name, help="初始化" if name == "setup" else "登记另一块磁盘")
        sub.add_argument("--device", help="明确选择 /dev/sdXN")
        sub.add_argument("--name", help="共享名，例如 family")
        sub.add_argument("--writable", action="store_true", help="为支持的文件系统启用写入；默认只读")
        sub.add_argument("--apfs-volume", type=int, help="明确选择 APFS 容器内的卷编号")
        sub.add_argument("--claim-root", action="store_true",
                         help="仅限可写 ext4/XFS：用户不能写入且卷根目录只有 lost+found 时，把根目录本身交给共享用户（0700，不递归）")
        if name == "setup":
            sub.add_argument("--user", help="普通 Linux 用户")
            sub.add_argument("--interface", help="家庭网络接口")
    for name in ("pause", "resume", "remove", "guard"):
        sub = commands.add_parser(name)
        sub.add_argument("name")
    for name in ("status", "reconcile", "stop", "start", "passwd"):
        commands.add_parser(name)
    commands.add_parser("upgrade", help="用新的 pyz 更新程序和服务文件并重启运行中的服务；不改配对、凭据和证书")
    network = commands.add_parser("network", help="家庭网卡或网段变化后重新登记；之后需 gateway-init --replace")
    network.add_argument("--interface", help="家庭网络接口")
    web = commands.add_parser("serve", help="运行本机状态页面")
    web.add_argument("--no-browser", action="store_true")
    web.add_argument("--demo", action="store_true", help="显示明确标注的演示数据；无磁盘操作")
    web.add_argument("--port", type=int, default=8787)
    gateway = commands.add_parser("gateway-init", help="配对 Family Dashboard 并启用 LAN HTTPS 文件网关")
    gateway.add_argument("--family", required=True, help="FD 设置页显示的家庭 ID")
    gateway.add_argument("--origin", required=True, help="FD 的 HTTPS 网站源")
    gateway.add_argument("--address", help="当前家庭网卡 IPv4 地址")
    gateway.add_argument("--replace", action="store_true", help="轮换配对凭据，旧凭据立即失效")
    commands.add_parser("gateway-renew", help="用原 CA 续期网关服务器证书；配对凭据不变")
    commands.add_parser("gateway-serve", help="以普通用户运行文件网关（通常由 systemd 调用）")
    return p


def main():
    args = parser().parse_args()
    try:
        if args.command == "scan":
            devices = c.inventory()
            print(json.dumps(devices, ensure_ascii=False, indent=2)) if args.json else table(devices)
        elif args.command == "doctor":
            doctor()
        elif args.command == "setup":
            setup(args)
        elif args.command == "add":
            add(args)
        elif args.command == "status":
            from .web import snapshot
            print(json.dumps(snapshot(), ensure_ascii=False, indent=2))
        elif args.command == "serve":
            from .web import serve
            serve(args.port, args.demo, not args.no_browser)
        elif args.command == "guard":
            c.guard(args.name)
        elif args.command == "reconcile":
            root()
            print(json.dumps(c.reconcile(), ensure_ascii=False, indent=2))
        elif args.command == "pause":
            disable_share(args.name)
        elif args.command == "remove":
            disable_share(args.name, forget=True)
        elif args.command == "resume":
            enable_share(args.name)
        elif args.command == "stop":
            stop()
        elif args.command == "start":
            start()
        elif args.command == "passwd":
            root()
            set_password(c.load_config())
        elif args.command == "upgrade":
            upgrade()
        elif args.command == "network":
            renetwork(args)
        elif args.command == "gateway-init":
            from .gateway_setup import initialize
            initialize(args)
        elif args.command == "gateway-renew":
            from .gateway_setup import renew
            renew()
        elif args.command == "gateway-serve":
            from .gateway import serve
            serve()
    except (c.NasError, OSError, ValueError, KeyError) as e:
        print(f"\n未完成：{e}", file=sys.stderr)
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        print("\n已中止。可运行 status / doctor 查看当前状态。", file=sys.stderr)
        sys.exit(130)
