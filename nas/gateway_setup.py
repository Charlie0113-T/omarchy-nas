"""Local, privileged enrollment for the unprivileged HTTPS gateway."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import secrets
import shutil
import tempfile
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
NoNewPrivileges=true
CapabilityBoundingSet=
AmbientCapabilities=
RestrictSUIDSGID=true
UMask=0077
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


def certificates(address, cfg):
    tls = c.P.etc / "tls"
    c.secure_dir(tls, 0o750)
    os.chown(tls, 0, cfg["gid"])
    # CA key stays root-only; only the gateway leaf key is readable by its user.
    if (tls / "ca.crt").exists() and not (tls / "ca.key").exists():
        raise c.NasError("已有 CA 证书但私钥缺失；请恢复原 ca.key，或明确安排所有设备重新信任新的 CA。")
    if not (tls / "ca.key").exists():
        c.run(["openssl", "req", "-x509", "-newkey", "rsa:3072", "-nodes", "-sha256",
               "-days", "3650", "-subj", "/CN=Family NAS Local CA/",
               "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
               "-addext", "keyUsage=critical,keyCertSign,cRLSign",
               "-keyout", str(tls / "ca.key"), "-out", str(tls / "ca.crt")], timeout=60)
        os.chmod(tls / "ca.key", 0o600)
    if not (tls / "ca.crt").exists():
        raise c.NasError("缺少 CA 证书；保留 ca.key 并先修复证书，不自动替换设备已信任的 CA。")
    with tempfile.TemporaryDirectory(prefix="leaf-", dir=tls) as tmp:
        tmp = Path(tmp)
        c.atomic(tmp / "extensions", f"subjectAltName=IP:{address}\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n")
        c.run(["openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes", "-sha256",
               "-subj", "/CN=Family NAS/", "-keyout", str(tmp / "key"), "-out", str(tmp / "csr")], timeout=60)
        c.run(["openssl", "x509", "-req", "-in", str(tmp / "csr"), "-CA", str(tls / "ca.crt"),
               "-CAkey", str(tls / "ca.key"), "-set_serial", str(secrets.randbits(128)),
               "-days", "365", "-sha256", "-extfile", str(tmp / "extensions"), "-out", str(tmp / "crt")])
        for source, target in (("key", "server.key"), ("crt", "server.crt")):
            os.chmod(tmp / source, 0o640)
            os.chown(tmp / source, 0, cfg["gid"])
            os.replace(tmp / source, tls / target)
    # Public certificate; safe to transfer to family devices for explicit trust.
    c.atomic(c.P.etc / "family-nas-ca.crt", (tls / "ca.crt").read_text())


def initialize(args):
    from .cli import root, copy_app
    root()
    cfg = c.load_config()
    family_id = str(uuid.UUID(args.family))
    allowed_origin = origin(args.origin)
    if not shutil.which("openssl", path=c.SAFE_PATH):
        raise c.NasError("请先安装 openssl：sudo pacman -S --needed openssl")
    addresses = [i["ip"] for i in c.network_interfaces() if i["name"] == cfg["interface"]
                 and ipaddress.ip_address(i["ip"]) in ipaddress.ip_network(cfg["network"])]
    address = args.address or (addresses[0] if len(addresses) == 1 else None)
    if address not in addresses:
        raise c.NasError("请用 --address 明确选择已登记家庭接口上当前的私有 IPv4 地址。")
    shares = [s["name"] for s in cfg["shares"] if s["enabled"]]
    if not shares:
        raise c.NasError("请先 setup / resume 至少一个共享卷。")
    gateway_path = c.P.etc / "gateway.json"
    if gateway_path.exists() and not args.replace:
        raise c.NasError("已配对。重新配对需加 --replace，旧配对凭据会失效；设备需导入新文件。")
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
    c.run(["systemctl", "is-active", "--quiet", SERVICE])
    print(f"HTTPS 网关已启动：{pairing['baseUrl']}\n允许 FD 网站：{allowed_origin}\n允许家庭网段：{cfg['network']}\n共享：{', '.join(shares)}")
    print("\n在本机文件管理器打开 /etc/omarchy-nas/：\n"
          "1. 将 family-nas-ca.crt 安装为各设备信任的根证书（不要转移 ca.key/server.key）。\n"
          "2. 在 FD → 设置 → 家庭文件导入 pairing.json。配对文件含访问凭据，请勿分享给家庭以外的人。\n"
          "3. 浏览器询问本地网络权限时允许。建议在路由器为 NAS 保留当前 IP。\n"
          "网关证书有效期 1 年；续期、地址或网站更改时以 --replace 重新生成配对。")
