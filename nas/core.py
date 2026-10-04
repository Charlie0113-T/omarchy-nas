from __future__ import annotations

import contextlib
import errno
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass

from . import __version__ as VERSION
APFS_GUID = "7c3457ef-0000-11aa-aa11-00306543ecac"
SUPPORTED = {"ext4", "xfs", "exfat", "vfat", "ntfs", "apfs"}
PRIVATE = tuple(ipaddress.ip_network(x) for x in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
SERVICE = "omarchy-nas-smb.service"
PYTHON = "/usr/bin/python3"
UFW = Path("/etc/ufw/ufw.conf")
# Defence in depth for the LAN gateway and the desktop-user panel. Keeps /srv, /dev and
# /sys visible: verify_mount resolves /dev/disk/by-* and lsblk reads sysfs.
HARDENING = """NoNewPrivileges=true
CapabilityBoundingSet=
AmbientCapabilities=
RestrictSUIDSGID=true
ProtectHome=yes
ProtectSystem=full
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictNamespaces=yes
LockPersonality=yes
RestrictRealtime=yes
SystemCallArchitectures=native
InaccessiblePaths=-/run/docker.sock
LimitNOFILE=1024
TasksMax=256
"""


class NasError(Exception):
    pass


@dataclass(frozen=True)
class Paths:
    etc: Path = Path("/etc/omarchy-nas")
    data: Path = Path("/var/lib/omarchy-nas")
    mounts: Path = Path("/srv/omarchy-nas")
    run: Path = Path("/run/omarchy-nas")
    app: Path = Path("/opt/omarchy-nas/omarchy-nas.pyz")
    units: Path = Path("/etc/systemd/system")

    @property
    def config(self):
        return self.etc / "config.json"

    @property
    def smb(self):
        return self.etc / "smb.conf"


P = Paths()


def tool(name):
    path = shutil.which(name, path=SAFE_PATH)
    if not path:
        raise NasError(f"缺少命令 {name}。安装依赖后重试。")
    if os.geteuid() == 0:
        info = Path(path).resolve().stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise NasError(f"拒绝以 root 执行非 root 所有或可被其他用户改写的程序：{path}")
    return path


def run(args, *, check=True, timeout=30, input=None):
    args = [str(x) for x in args]
    args[0] = tool(args[0]) if "/" not in args[0] else args[0]
    try:
        result = subprocess.run(args, text=True, capture_output=True, timeout=timeout,
                                input=input, stdin=subprocess.DEVNULL if input is None else None,
                                env={"PATH": SAFE_PATH, "LC_ALL": "C", "LANG": "C"})
    except (OSError, subprocess.TimeoutExpired) as e:
        raise NasError(f"{Path(args[0]).name} 无法完成：{e}") from e
    if check and result.returncode:
        raise NasError(f"{Path(args[0]).name} 失败（{result.returncode}）：\n{(result.stderr or result.stdout).strip()[:3000]}")
    return result


def jrun(args):
    try:
        return json.loads(run(args).stdout)
    except ValueError as e:
        raise NasError(f"无法解析 {args[0]} 输出") from e


def clean(value):
    # Device labels are data, never terminal control codes or configuration lines.
    return "".join(c for c in str(value or "") if c.isprintable())[:150]


def valid_name(value):
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", value or "") or value in {"global", "homes", "printers", "ipc"}:
        raise NasError("共享名须以小写字母开头，最长 32 位，仅用字母、数字、-、_。")
    return value


def inside(path, base):
    return path == str(base) or path.startswith(str(base) + "/")


def inventory(raw=None, p=P):
    if raw is None:
        raw = jrun(["lsblk", "--json", "--bytes", "--paths", "--tree", "--output",
                    "NAME,PATH,TYPE,SIZE,FSTYPE,LABEL,UUID,PARTUUID,PARTTYPE,MODEL,SERIAL,TRAN,RM,RO,MOUNTPOINTS,MAJ:MIN"])
    rows = []

    def walk(node, disk=None, ancestry=()):
        disk = node if node.get("type") == "disk" else disk
        disk = disk or node
        fs = (node.get("fstype") or "").lower()
        if not fs and (node.get("parttype") or "").lower() == APFS_GUID:
            fs = "apfs"
        mounts = [m for m in (node.get("mountpoints") or []) if m]
        row = dict(path=node.get("path") or node["name"], type=node.get("type"),
                   size=int(node.get("size") or 0), fs=fs, label=clean(node.get("label")),
                   uuid=node.get("uuid") or "", partuuid=node.get("partuuid") or "",
                   serial=disk.get("serial") or "", model=clean(disk.get("model")),
                   disk=disk.get("path") or disk["name"], transport=disk.get("tran") or "",
                   removable=bool(disk.get("rm")), readonly=bool(node.get("ro")),
                   mounts=mounts, major_minor=node.get("maj:min"),
                   ancestors=list(ancestry), has_children=bool(node.get("children")))
        rows.append(row)
        for child in node.get("children", []):
            walk(child, disk, ancestry + (row["path"],))

    for disk in raw.get("blockdevices", []):
        walk(disk)
    protected = set()
    for row in rows:
        if any(m == "[SWAP]" or m == "/" or any(inside(m, x) for x in ("/boot", "/efi", "/usr", "/var", "/home")) for m in row["mounts"]):
            protected.add(row["disk"])
    for row in rows:
        reasons = []
        if row["disk"] in protected:
            reasons.append("系统盘或含活动系统/交换分区")
        if row["transport"] != "usb" and not row["removable"]:
            reasons.append("首版仅接入 USB / 可移除磁盘")
        if row["type"] not in {"disk", "part"} or row["has_children"]:
            reasons.append("不是可直接接入的独立文件系统")
        if row["fs"] not in SUPPORTED:
            reasons.append("文件系统暂不支持" + (f"：{row['fs']}" if row["fs"] else "或尚未识别"))
        if not row["uuid"] and not row["partuuid"]:
            reasons.append("没有稳定 UUID / PARTUUID")
        if any(not inside(m, p.mounts) for m in row["mounts"]):
            reasons.append("已由其他程序挂载，请先正常卸载")
        row["eligible"] = not reasons
        row["reason"] = "；".join(reasons)
    # Shared/multipath blocks can appear more than once. Do not hide that ambiguity.
    unique = {}
    for row in rows:
        if row["path"] in unique:
            unique[row["path"]]["eligible"] = False
            unique[row["path"]]["reason"] = "设备有多个父节点，需人工检查"
        else:
            unique[row["path"]] = row
    return list(unique.values())


def network_interfaces(raw=None):
    if raw is None:
        raw = jrun(["ip", "-j", "-4", "addr", "show", "up"])
    found = []
    for iface in raw:
        name = iface.get("ifname", "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", name) or name == "lo":
            continue
        for addr in iface.get("addr_info", []):
            if addr.get("family") != "inet":
                continue
            ip = ipaddress.ip_address(addr["local"])
            network = ipaddress.ip_network(f"{ip}/{addr['prefixlen']}", strict=False)
            if any(network.subnet_of(net) for net in PRIVATE):
                found.append({"name": name, "ip": str(ip), "network": str(network)})
    return found


def validate_config(cfg):
    if cfg.get("schema") != 1:
        raise NasError("配置版本不受支持")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", cfg.get("user", "")) or cfg.get("uid", 0) < 1000:
        raise NasError("共享必须使用普通 Linux 用户")
    if not isinstance(cfg.get("gid"), int) or cfg["gid"] < 0:
        raise NasError("无效 gid")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", cfg.get("interface", "")):
        raise NasError("无效网络接口")
    try:
        network = ipaddress.ip_network(cfg["network"])
        if network.version != 4 or not any(network.subnet_of(net) for net in PRIVATE):
            raise ValueError("仅支持家庭私有 IPv4 网段")
    except (ValueError, KeyError, TypeError) as e:
        raise NasError(f"无效家庭网段：{e}") from e
    seen = set()
    identities = set()
    if len(cfg.get("shares", [])) > 32:
        raise NasError("首版最多支持 32 个共享")
    for s in cfg.get("shares", []):
        valid_name(s["name"])
        key = (s.get("id_type"), s.get("id"))
        if s["name"] in seen or key in identities:
            raise NasError("共享名或卷身份重复；首版每容器只接入一个卷")
        seen.add(s["name"])
        identities.add(key)
        if key[0] not in {"uuid", "partuuid"} or not re.fullmatch(r"[A-Za-z0-9-]{1,80}", key[1] or ""):
            raise NasError("无效设备标识")
        if s.get("fs") not in SUPPORTED or s.get("mode") not in {"ro", "rw"}:
            raise NasError("无效文件系统 / 共享模式")
        if s["fs"] == "apfs" and s["mode"] != "ro":
            raise NasError("APFS 仅支持只读")
        if not isinstance(s.get("volume", 0), int) or not 0 <= s.get("volume", 0) <= 255:
            raise NasError("APFS 卷编号无效")
        if not isinstance(s.get("enabled"), bool):
            raise NasError("缺少启用状态")
    return cfg


def load_config(p=P):
    try:
        return validate_config(json.loads(p.config.read_text()))
    except FileNotFoundError as e:
        raise NasError("尚未设置。请先运行 setup。") from e


def secure_dir(path, mode=0o755):
    path = Path(path)
    # Refuse symlinked or user-writable parents of privileged state.
    for ancestor in reversed((path,) + tuple(path.parents)):
        if ancestor.exists() or ancestor.is_symlink():
            st = ancestor.lstat()
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                raise NasError(f"目录不安全：{ancestor}")
            if os.geteuid() == 0 and (st.st_uid != 0 or st.st_mode & 0o022):
                raise NasError(f"目录非 root 所有或可被他人改写：{ancestor}")
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    os.chmod(path, mode)


def atomic(path, content, mode=0o644):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=".nas-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            os.fchmod(out.fileno(), mode)
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


def save_config(cfg, p=P):
    validate_config(cfg)
    atomic(p.config, json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")


@contextlib.contextmanager
def locked(p=P):
    secure_dir(p.run)
    with open(p.run / "manager.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def match_device(share, devices):
    matches = [d for d in devices if d.get(share["id_type"]) == share["id"]]
    if len(matches) != 1:
        raise NasError("磁盘离线" if not matches else "标识重复，拒绝自动接入")
    d = matches[0]
    if not d["eligible"]:
        raise NasError(d["reason"])
    if d["fs"] != share["fs"] or d["size"] != share["size"]:
        raise NasError("文件系统或容量与登记记录不同")
    if share.get("serial") and d.get("serial") != share["serial"]:
        raise NasError("硬盘序列号发生变化")
    if share["mode"] == "rw" and d["readonly"]:
        raise NasError("磁盘处于硬件只读状态")
    return d


def mount_command(share, device, cfg, p=P):
    target = str(p.mounts / valid_name(share["name"]))
    source = f"/dev/disk/by-{share['id_type']}/{share['id']}"
    mode, fs = share["mode"], share["fs"]
    opts = [mode, "nosuid", "nodev", "noexec"]
    if fs == "apfs":
        if mode != "ro":
            raise NasError("拒绝 APFS 写入")
        # apfs-fuse records its device argument as the mount source. Pass the kernel node, not the
        # by-id link, so a stale daemon left by a replug cannot pass verify_mount via a re-pointed link.
        node = os.path.realpath(device["path"])
        if os.path.realpath(source) != node:
            raise NasError(f"{source} 未指向登记的设备 {device['path']}；拒绝挂载")
        opts += ["allow_other", "default_permissions", "subtype=omarchy-apfs", f"uid={cfg['uid']}", f"gid={cfg['gid']}"]
        return ["apfs-fuse", "-v", str(share.get("volume", 0)), "-o", ",".join(opts), node, target]
    if mode == "ro":
        opts += {"ext4": ["noload"], "xfs": ["norecovery"]}.get(fs, [])
    if fs in {"ntfs", "exfat", "vfat"}:
        opts += [f"uid={cfg['uid']}", f"gid={cfg['gid']}", "umask=0077"]
    if fs == "ntfs":
        # No force/remove_hiberfile or journal recovery on an existing Windows volume.
        opts += ["norecover"]
        return ["ntfs-3g", "-o", ",".join(opts), source, target]
    return ["mount", "-t", fs, "-o", ",".join(opts), "--", source, target]


def mount_info(target):
    out = run(["findmnt", "--json", "--mountpoint", str(target), "--output", "TARGET,SOURCE,FSTYPE,OPTIONS,MAJ:MIN"], check=False)
    if out.returncode == 1:
        return None
    if out.returncode:
        raise NasError("读取挂载表失败：" + out.stderr.strip())
    entries = json.loads(out.stdout).get("filesystems", [])
    if len(entries) > 1 or any(e.get("children") for e in entries):
        raise NasError(f"挂载点 {target} 存在多层挂载，需人工检查；未改动任何目录")
    return entries[0] if entries else None


def verify_mount(share, device, p=P):
    info = mount_info(p.mounts / share["name"])
    if not info:
        raise NasError("共享目录没有真实挂载；已阻止回退到系统盘目录")
    opts = set(info.get("options", "").split(","))
    expected_fs = {"apfs": {"fuse.omarchy-apfs"}, "ntfs": {"fuseblk", "fuse.ntfs-3g"}}.get(share["fs"], {share["fs"]})
    if info.get("fstype") not in expected_fs:
        raise NasError("实际挂载文件系统不匹配")
    source, mismatch = info.get("source") or "", ""
    if share["fs"] in {"apfs", "ntfs"}:
        if not source.startswith("/dev/") or os.path.realpath(source) != os.path.realpath(device["path"]):
            mismatch = "FUSE 挂载源未能核实；拒绝共享"
    elif info.get("maj:min") != device["major_minor"]:
        mismatch = "挂载设备与登记设备不一致"
    if mismatch:
        # A replug leaves the old mount on its vanished node while the disk returns under a new one.
        # findmnt shows bind mounts as /dev/sdX[/dir]; those are a different mount, not a vanished node.
        if source.startswith("/dev/") and "[" not in source and not os.path.exists(source):
            raise NasError(f"陈旧挂载：{share['name']} 的磁盘已断开后重新接入（挂载仍指向已消失的 {clean(source)}，当前为 {device['path']}）。"
                           f"请先 sudo omarchy-nas pause {share['name']} 再 resume {share['name']}。")
        raise NasError(mismatch)
    if share["mode"] not in opts or not {"nosuid", "nodev", "noexec"}.issubset(opts):
        raise NasError("实际挂载选项与共享策略不一致")
    return info


def probe(target, name):
    # A FUSE daemon whose disk vanished still owns the mount; report that, not a permission error.
    try:
        os.statvfs(target)
        with os.scandir(target) as entries:
            next(entries, None)
    except OSError as e:
        if e.errno in {errno.ENOTCONN, errno.EIO}:
            raise NasError(f"陈旧挂载：{name} 的磁盘已断开或重新接入（{e.strerror}）。请先 sudo omarchy-nas pause {name} 再 resume {name}。") from e
        raise


def user_access(cfg, target, mode):
    # Check actual filesystem access as the SMB user, without writing test files.
    check = "import os,sys; p=sys.argv[1]; required=os.R_OK|os.X_OK; required|=os.W_OK if sys.argv[2]=='rw' else 0; sys.exit(0 if os.access(p,required) else 1)"
    return run(["runuser", "-u", cfg["user"], "--", PYTHON, "-c", check, str(target), mode], check=False).returncode == 0


def claim_root(target, cfg):
    # Only a fresh ext4/XFS root (mkfs leaves root:root 0755 plus lost+found); never recursive.
    fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        if set(os.listdir(fd)) - {"lost+found"}:
            return False
        os.fchown(fd, cfg["uid"], cfg["gid"])
        os.fchmod(fd, 0o700)
        return True
    finally:
        os.close(fd)


def root_remedy(share, cfg, device, refused):
    owner = f"{cfg['uid']}:{cfg['gid']}"
    lines = [f"新建的 {share['fs']} 卷根目录默认属于 root。一次性处理，只改卷根目录本身，不递归：",
             "· 卷根目录除 lost+found 外已有内容，--claim-root 未做任何修改。" if refused else
             "· 卷内只有 lost+found：重新运行同一命令并加 --claim-root。",
             f"· 已有数据：sudo mount {device['path']} /mnt && sudo chown {owner} /mnt && sudo chmod 0700 /mnt && sudo umount /mnt，然后重试。"]
    if share["fs"] == "ext4":
        lines.append(f"· 以后新建上传分区时直接指定所有者：sudo mkfs.ext4 -E root_owner={owner} <新分区>（会清空该分区，只用于空白新分区）。")
    return "\n".join(lines)


def ensure_mounted(share, cfg, devices, p=P, *, claim=False):
    d = match_device(share, devices)
    target = p.mounts / share["name"]
    existing = mount_info(target)
    if not existing:
        # Only chmod a placeholder that findmnt reports unmounted and that is not itself a mount root.
        if target.exists() and os.lstat(target).st_dev != os.lstat(target.parent).st_dev:
            raise NasError(f"{target} 已是其他文件系统的挂载根目录，需人工检查；未改动权限")
        secure_dir(target, 0o000)
        if any(target.iterdir()):
            raise NasError("挂载目录非空，拒绝隐藏已有文件")
    try:
        if not existing:
            command = mount_command(share, d, cfg, p)
            if share["fs"] in {"apfs", "ntfs"}:
                # A FUSE daemon must not live inside the short-lived reconcile job's
                # cgroup: systemd would kill it when the oneshot job completes.
                # A transient forking service owns it until normal umount exits it.
                command[0] = tool(command[0])
                command = ["systemd-run", "--collect", f"--unit=omarchy-nas-volume-{share['name']}",
                           "--property=Type=forking", "--property=TimeoutStartSec=45", "--", *command]
            try:
                run(command, timeout=55)
            except NasError as e:
                detail = ""
                if share["fs"] in {"apfs", "ntfs"}:
                    with contextlib.suppress(NasError):
                        log = run(["journalctl", "-u", f"omarchy-nas-volume-{share['name']}.service",
                                   "--since=-2min", "-n", "15", "--no-pager", "-o", "cat"], check=False)
                        detail = "\n" + "\n".join(clean(line) for line in log.stdout.splitlines())
                raise NasError(str(e) + detail) from e
        verify_mount(share, d, p)
        probe(target, share["name"])
        if not user_access(cfg, target, share["mode"]):
            native = share["mode"] == "rw" and share["fs"] in {"ext4", "xfs"}
            claimed = native and claim and claim_root(target, cfg)
            if not (claimed and user_access(cfg, target, share["mode"])):
                message = f"用户 {cfg['user']} 没有卷根目录所需的{'读写' if share['mode'] == 'rw' else '读取'}权限；"
                if claimed:
                    raise NasError(message + "卷根目录已改为该用户所有（0700，未递归），仍无法访问，需人工检查挂载选项。")
                raise NasError(message + "原有文件权限保持不变。" + ("\n" + root_remedy(share, cfg, d, claim) if native else ""))
    except Exception:
        if not existing:
            with contextlib.suppress(NasError):
                run(["umount", "--", str(target)])
        raise
    return d


def smb_config(cfg, p=P):
    validate_config(cfg)
    lines = ["# Managed by Omarchy NAS. Existing /etc/samba/smb.conf is untouched.", "[global]",
             "server role = standalone server", "security = user", "map to guest = Never",
             "server min protocol = SMB3_00", "smb encrypt = required", "smb ports = 445",
             "disable netbios = yes", f"interfaces = lo {cfg['interface']}", "bind interfaces only = yes",
             f"hosts allow = 127.0.0.1 {cfg['network']}", "hosts deny = ALL",
             "load printers = no", "printing = bsd", "printcap name = /dev/null", "disable spoolss = yes",
             f"private dir = {p.data}/private", f"state directory = {p.data}/state",
             f"cache directory = {p.data}/cache", f"lock directory = {p.run}/samba",
             f"pid directory = {p.run}/samba", f"log file = {p.data}/logs/smb.%m.log",
             "max log size = 1000", f"passdb backend = tdbsam:{p.data}/private/passdb.tdb", ""]
    for share in cfg["shares"]:
        if not share["enabled"]:
            continue
        lines += [f"[{share['name']}]", f"path = {p.mounts}/{share['name']}",
                  "browseable = yes", "guest ok = no", f"valid users = {cfg['user']}",
                  f"force user = {cfg['user']}", f"read only = {'yes' if share['mode']=='ro' else 'no'}",
                  "follow symlinks = no", "wide links = no", "create mask = 0600", "directory mask = 0700",
                  f"root preexec = {PYTHON} {p.app} guard {share['name']}", "root preexec close = yes", ""]
    return "\n".join(lines)


def write_smb(cfg, p=P):
    candidate = p.etc / "smb.conf.next"
    atomic(candidate, smb_config(cfg, p))
    try:
        run(["testparm", "-s", str(candidate)])
        os.replace(candidate, p.smb)
    finally:
        candidate.unlink(missing_ok=True)


def reconcile(p=P):
    with locked(p):
        cfg = load_config(p)
        devices = inventory(p=p)
        statuses = []
        for share in cfg["shares"]:
            state = {"name": share["name"], "mode": share["mode"], "enabled": share["enabled"], "online": False}
            if share["enabled"]:
                try:
                    ensure_mounted(share, cfg, devices, p)
                    state["online"] = True
                except (NasError, OSError) as e:
                    state["error"] = str(e)
            statuses.append(state)
        atomic(p.run / "status.json", json.dumps({"at": time.time(), "shares": statuses}, ensure_ascii=False))
        return statuses


def guard(name, p=P):
    cfg = load_config(p)
    matches = [s for s in cfg["shares"] if s["name"] == valid_name(name) and s["enabled"]]
    if len(matches) != 1:
        raise NasError("共享未启用")
    d = match_device(matches[0], inventory(p=p))
    verify_mount(matches[0], d, p)
    probe(p.mounts / name, name)


def prepare_paths(p=P):
    for path in (p.etc, p.mounts, p.data, p.run, p.app.parent):
        secure_dir(path)
    for name in ("private", "state", "cache", "logs"):
        secure_dir(p.data / name, 0o700)
    secure_dir(p.run / "samba", 0o755)


def unit_files(cfg, p=P):
    app = f"{PYTHON} {p.app}"
    return {
        SERVICE: f"""[Unit]
Description=Omarchy NAS private Samba instance
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
RuntimeDirectory=omarchy-nas/samba
RuntimeDirectoryMode=0755
ExecStart=/usr/bin/smbd --foreground --no-process-group --configfile={p.smb}
ExecReload=/bin/kill -HUP $MAINPID
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
""",
        "omarchy-nas-reconcile.service": f"""[Unit]
Description=Reconnect enrolled Omarchy NAS volumes

[Service]
Type=oneshot
ExecStart={app} reconcile
TimeoutStartSec=1800
""",
        "omarchy-nas-reconcile.timer": """[Unit]
Description=Check enrolled NAS volumes every 20 seconds

[Timer]
OnBootSec=15
OnUnitInactiveSec=20
AccuracySec=1s
Unit=omarchy-nas-reconcile.service

[Install]
WantedBy=timers.target
""",
        "omarchy-nas-panel.service": f"""[Unit]
Description=Omarchy NAS local status panel
After=network.target

[Service]
Type=simple
User={cfg['user']}
ExecStart={app} serve --no-browser
Restart=on-failure
RestartSec=5
{HARDENING}
[Install]
WantedBy=multi-user.target
"""}


def cert_expiry(path):
    out = run(["openssl", "x509", "-noout", "-enddate", "-in", str(path)])
    return ssl.cert_time_to_seconds(out.stdout.strip().removeprefix("notAfter="))


def ufw_enabled(path=None):
    try:
        lines = (path or UFW).read_text().splitlines()
    except OSError:
        return None
    return any(line.replace('"', "").strip() == "ENABLED=yes" for line in lines)


def firewall_commands(network, address):
    # Printed for the admin only; this tool never changes the firewall.
    return [f"sudo ufw allow from {network} to {address} port {port} proto tcp" for port in (8789, 445)]


def human_size(size):
    n = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if n < 1024 or unit == "PiB":
            return f"{n:.1f} {unit}"
        n /= 1024
