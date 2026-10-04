"""Local, privileged enrollment for the unprivileged HTTPS gateway."""
from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import json
import os
import secrets
import shutil
import socket
import ssl
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from . import core as c
from .gateway import MAX_UPLOAD

SERVICE = "omarchy-nas-gateway.service"


def origin(value):
    url = urlsplit(value)
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.path not in {"", "/"} or url.query or url.fragment):
        raise c.NasError("FD origin 必须是 HTTPS 网站源，例如 https://fd.example.com（不含家庭路径）。")
    if not url.hostname.isascii() or any(ch.isspace() for ch in value):
        raise c.NasError("请使用网站的 ASCII 域名。")
    # Access .port to reject malformed ports.
    port = url.port
    return f"https://{url.hostname}" + (f":{port}" if port and port != 443 else "")


def unit(cfg):
    return f"""[Unit]
Description=Family Dashboard LAN file gateway (omarchy-nas)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={cfg['user']}
Group={cfg['gid']}
ExecStart={c.PYTHON} {c.P.app} gateway-serve
{c.HARDENING}RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK
UMask=0077
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


def name_constraints(cfg):
    # Family devices fully trust this CA. Limit it to private IPv4 leaves so a leaked ca.key cannot
    # mint certificates for real sites; DNS:invalid stops unconstrained DNS names (RFC 5280).
    networks = list(c.PRIVATE)
    home = ipaddress.ip_network(cfg["network"]) if cfg.get("network") else None
    if home and not any(home.subnet_of(n) for n in networks):
        networks.append(home)
    permitted = [f"permitted;IP:{n.network_address}/{n.netmask}" for n in networks] + ["permitted;DNS:invalid"]
    return "nameConstraints=critical," + ",".join(permitted)


def day(seconds):
    return time.strftime("%Y-%m-%d", time.localtime(seconds))


def ca_rotation(tls):
    # Renewal reuses the CA; only moving it aside lets gateway-init create a new one. Nothing is deleted.
    stamp = time.strftime("%Y%m%d")
    return (f"轮换 CA：把 {tls} 中仍存在的 ca.key 与 ca.crt 改名保存（sudo mv -n {tls}/ca.key {tls}/ca.key.old-{stamp}；"
            f"sudo mv -n {tls}/ca.crt {tls}/ca.crt.old-{stamp}；不要删除），再在 FD 设置页复制 gateway-init 命令并加 --replace 运行，"
            "生成新 CA 与新配对。之后每台设备都必须重新信任新的 family-nas-ca.crt，并导入新的 pairing.json。")


def certificates(address, cfg):
    tls = c.P.etc / "tls"
    c.secure_dir(tls, 0o750)
    os.chown(tls, 0, cfg["gid"])
    # CA key stays root-only; only the gateway leaf key is readable by its user.
    if (tls / "ca.crt").exists() and not (tls / "ca.key").exists():
        raise c.NasError("已有 CA 证书但私钥缺失；请恢复原 ca.key，或明确安排所有设备重新信任新的 CA。")
    if not (tls / "ca.key").exists():
        c.run(["openssl", "req", "-x509", "-newkey", "rsa:3072", "-nodes", "-sha256",
               "-days", "1825", "-subj", "/CN=Family NAS Local CA/",
               "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
               "-addext", "keyUsage=critical,keyCertSign,cRLSign",
               "-addext", name_constraints(cfg),
               "-keyout", str(tls / "ca.key"), "-out", str(tls / "ca.crt")], timeout=60)
        os.chmod(tls / "ca.key", 0o600)
    if not (tls / "ca.crt").exists():
        raise c.NasError("缺少 CA 证书；保留 ca.key 并先修复证书，不自动替换设备已信任的 CA。")
    # Devices reject the whole chain once the CA expires, so the leaf never outlives it.
    ca_expires = c.cert_expiry(tls / "ca.crt")
    ca_days = int((ca_expires - time.time()) // 86400)
    days = min(365, ca_days - 1)
    if days < 1:
        raise c.NasError(f"家庭 CA 证书{'已于' if ca_expires < time.time() else '将于'} {day(ca_expires)} 到期，"
                         f"不能再签发设备会信任的网关证书，续期无效。\n{ca_rotation(tls)}")
    if ca_days < 400:
        print(f"注意：家庭 CA 证书将于 {day(ca_expires)} 到期（剩 {ca_days} 天），网关证书只签发 {days} 天。\n{ca_rotation(tls)}")
    with tempfile.TemporaryDirectory(prefix="leaf-", dir=tls) as tmp:
        tmp = Path(tmp)
        c.atomic(tmp / "extensions", f"subjectAltName=IP:{address}\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n")
        c.run(["openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes", "-sha256",
               "-subj", "/CN=Family NAS/", "-keyout", str(tmp / "key"), "-out", str(tmp / "csr")], timeout=60)
        c.run(["openssl", "x509", "-req", "-in", str(tmp / "csr"), "-CA", str(tls / "ca.crt"),
               "-CAkey", str(tls / "ca.key"), "-set_serial", str(secrets.randbits(128)),
               "-days", str(days), "-sha256", "-extfile", str(tmp / "extensions"), "-out", str(tmp / "crt")])
        for source, target in (("key", "server.key"), ("crt", "server.crt")):
            os.chmod(tmp / source, 0o640)
            os.chown(tmp / source, 0, cfg["gid"])
            os.replace(tmp / source, tls / target)
    # Public certificate; safe to transfer to family devices for explicit trust.
    c.atomic(c.P.etc / "family-nas-ca.crt", (tls / "ca.crt").read_text())


def allowlist(cfg):
    # Bind each paired name to the enrolled volume identity; paused shares stay paired but offline.
    if not any(s["enabled"] for s in cfg["shares"]):
        raise c.NasError("请先 setup / resume 至少一个共享卷。")
    return [{key: s[key] for key in ("name", "id_type", "id", "fs", "mode")} for s in cfg["shares"]]


def wait_ready(address, port=8789, timeout=15, tls=None):
    # Type=simple reports active at fork and another program may hold the port: only a TLS
    # handshake that presents this host's current server.crt proves this gateway started.
    tls = tls or c.P.etc / "tls"
    expected = ssl.PEM_cert_to_DER_cert((tls / "server.crt").read_text())
    # Pin the exact certificate instead of verifying the chain, so an expired leaf or CA reports
    # as expired below rather than as a gateway that never started.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((address, port), timeout=2) as sock, \
                    context.wrap_socket(sock, server_hostname=address) as peer:
                presented = peer.getpeercert(binary_form=True)
            if presented == expected and c.run(["systemctl", "is-active", "--quiet", SERVICE], check=False).returncode == 0:
                warn_expired(tls)
                return
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    log = ""
    with contextlib.suppress(c.NasError):
        out = c.run(["journalctl", "-u", SERVICE, "-n", "20", "--no-pager", "-o", "cat"], check=False)
        log = "\n".join(c.clean(line) for line in out.stdout.splitlines())
    raise c.NasError(f"网关未在 {timeout} 秒内就绪（{address}:{port}）。最近日志：\n{log or '（无）'}")


def warn_expired(tls):
    now = time.time()
    with contextlib.suppress(c.NasError, OSError, ValueError):
        if c.cert_expiry(tls / "ca.crt") < now:
            print("警告：家庭 CA 证书已到期，所有设备都会拒绝连接；需要按 gateway-renew 给出的步骤轮换 CA。")
        elif c.cert_expiry(tls / "server.crt") < now:
            print("警告：网关服务器证书已到期，FD 无法连接；运行 sudo omarchy-nas gateway-renew 续期（无需重新配对）。")


def firewall_note(network, address):
    commands = c.firewall_commands(network, address)
    note = f"防火墙只向家庭网段放行（本程序不会修改防火墙）：\n   {commands[0]}\n   {commands[1]}   # SMB"
    if c.ufw_enabled():
        note += "\n注意：UFW 已启用（/etc/ufw/ufw.conf ENABLED=yes）。放行前家庭设备连不上 8789 / 445。"
    return note


def renew():
    from .cli import root, service_active
    root()
    cfg = c.load_config()
    try:
        gateway = json.loads((c.P.etc / "gateway.json").read_text())
    except FileNotFoundError as e:
        raise c.NasError("尚未配对；请先运行 gateway-init。") from e
    tls = c.P.etc / "tls"
    if not (tls / "ca.key").exists() or not (tls / "ca.crt").exists():
        raise c.NasError(f"续期需要设备已信任的原有 tls/ca.key 与 tls/ca.crt；请先从备份恢复。无法恢复时只能轮换 CA。\n{ca_rotation(tls)}")
    # Reissue only server.crt/server.key; tokenHash, gatewayId and pairing.json stay as they are.
    old_mask = os.umask(0o077)
    try:
        certificates(gateway["bind"], cfg)
    finally:
        os.umask(old_mask)
    state = "网关当前未运行（例如已 stop）；运行 sudo omarchy-nas start 后使用新证书。"
    if service_active(SERVICE):
        c.run(["systemctl", "restart", SERVICE])
        wait_ready(gateway["bind"], gateway["port"])
        state = f"网关已重启：https://{gateway['bind']}:{gateway['port']}"
    ca_expires = c.cert_expiry(tls / "ca.crt")
    print(f"网关证书已续期至 {day(c.cert_expiry(tls / 'server.crt'))}。"
          f"家庭 CA 证书 {day(ca_expires)} 到期（剩 {int((ca_expires - time.time()) // 86400)} 天），续期不能超过它。{state}\n"
          f"配对凭据与 pairing.json 未改变，设备无需重新导入。\n{firewall_note(gateway['network'], gateway['bind'])}")


def initialize(args):
    from .cli import root, copy_app
    root()
    cfg = c.load_config()
    family_id = str(uuid.UUID(args.family))
    allowed_origin = origin(args.origin)
    if not shutil.which("openssl", path=c.SAFE_PATH):
        raise c.NasError("请先安装 openssl：sudo pacman -S --needed openssl")
    interfaces = c.network_interfaces()
    addresses = [i["ip"] for i in interfaces if i["name"] == cfg["interface"]
                 and ipaddress.ip_address(i["ip"]) in ipaddress.ip_network(cfg["network"])]
    address = args.address or (addresses[0] if len(addresses) == 1 else None)
    if address not in addresses:
        present = "；".join(f"{i['name']} {i['ip']}（{i['network']}）" for i in interfaces) or "没有私有 IPv4 接口"
        raise c.NasError(f"已登记的家庭接口 {cfg['interface']} · {cfg['network']} 上没有{'地址 ' + args.address if args.address else '唯一的当前地址'}。\n"
                         f"当前接口：{present}\n同一接口有多个地址时用 --address 选择；接口或网段已变化时先运行 sudo omarchy-nas network，再加 --replace 重新运行本命令。")
    shares = allowlist(cfg)
    gateway_path = c.P.etc / "gateway.json"
    if gateway_path.exists() and not args.replace:
        raise c.NasError("已配对。只更新程序版本请运行 sudo python3 ~/Downloads/omarchy-nas.pyz upgrade（新版本 pyz）；只续期证书用 sudo omarchy-nas gateway-renew。\n"
                         "重新配对需加 --replace，旧配对凭据会失效；设备需导入新文件。")
    existing_unit = c.P.units / SERVICE
    if existing_unit.exists() and "omarchy-nas" not in existing_unit.read_text():
        raise c.NasError("系统已有同名文件网关服务；未覆盖它。")
    c.prepare_paths()
    copy_app()
    # Protect keys at creation, including the first OpenSSL write.
    old_mask = os.umask(0o077)
    try:
        certificates(address, cfg)
        token = secrets.token_urlsafe(32)
        gateway_id = str(uuid.uuid4())
        config = {"protocolVersion": 1, "gatewayId": gateway_id, "familyId": family_id,
                  "bind": address, "port": 8789, "network": cfg["network"],
                  "hosts": [f"{address}:8789"], "origins": [allowed_origin], "shares": shares,
                  "maxUploadBytes": MAX_UPLOAD, "tokenHash": hashlib.sha256(token.encode()).hexdigest()}
        pairing = {"version": 1, "baseUrl": f"https://{address}:8789", "familyId": family_id,
                   "gatewayId": gateway_id, "token": token}
        for path, value in ((gateway_path, config), (c.P.etc / "pairing.json", pairing)):
            c.atomic(path, json.dumps(value, indent=2) + "\n", mode=0o640)
            os.chown(path, 0, cfg["gid"])
        c.atomic(c.P.units / SERVICE, unit(cfg))
    finally:
        os.umask(old_mask)
    c.run(["systemctl", "daemon-reload"])
    c.run(["systemctl", "enable", SERVICE])
    c.run(["systemctl", "restart", SERVICE])
    wait_ready(address)
    names = ", ".join(s["name"] + ("" if s["enabled"] else "（已暂停）") for s in cfg["shares"])
    print(f"HTTPS 网关已启动：{pairing['baseUrl']}\n允许 FD 网站：{allowed_origin}\n允许家庭网段：{cfg['network']}\n共享：{names}")
    print("\n在本机文件管理器打开 /etc/omarchy-nas/：\n"
          "1. 将 family-nas-ca.crt 安装为各设备信任的根证书（不要转移 ca.key/server.key）。\n"
          "2. 在 FD → 设置 → 家庭文件导入 pairing.json。配对文件含访问凭据，请勿分享给家庭以外的人。\n"
          "3. 浏览器询问本地网络权限时允许。建议在路由器为 NAS 保留当前 IP。\n"
          f"4. {firewall_note(cfg['network'], address)}\n"
          "网关证书有效期最长 1 年，不超过家庭 CA 的到期日；到期前用 sudo omarchy-nas gateway-renew 续期，配对不变。地址或网站更改时以 --replace 重新生成配对。")
