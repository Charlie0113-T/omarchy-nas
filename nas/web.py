from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.resources
import json
import os
import socket
import threading
import time
import webbrowser

from . import core as c


def snapshot():
    result = {"version": c.VERSION, "hostname": socket.gethostname(), "time": time.time(),
              "demo": False, "errors": [], "devices": [], "shares": [], "interfaces": [],
              "configured": False, "service_active": False}
    try:
        result["devices"] = c.inventory()
    except (c.NasError, ValueError, OSError) as e:
        result["errors"].append(str(e))
    try:
        result["interfaces"] = c.network_interfaces()
    except (c.NasError, ValueError, OSError) as e:
        result["errors"].append(str(e))
    if c.P.config.exists():
        try:
            cfg = c.load_config()
            result.update(configured=True, user=cfg["user"], interface=cfg["interface"], network=cfg["network"])
            result["service_active"] = c.run(["systemctl", "is-active", "--quiet", c.SERVICE], check=False).returncode == 0
            cached = {"at": 0, "shares": []}
            try:
                cached = json.loads((c.P.run / "status.json").read_text())
            except (FileNotFoundError, ValueError):
                pass
            result["sample_age"] = time.time() - cached["at"]
            for share in cfg["shares"]:
                out = {**share, "online": False}
                previous = next((s for s in cached["shares"] if s["name"] == share["name"]), {})
                if not share["enabled"]:
                    out["state"] = "paused"
                else:
                    try:
                        dev = c.match_device(share, result["devices"])
                        c.verify_mount(share, dev)
                        if not result["service_active"]:
                            raise c.NasError("共享服务未运行")
                        if not previous.get("online") or result["sample_age"] > 180:
                            raise c.NasError(previous.get("error") or "等待后台核验 / 状态采样过期")
                        out.update(online=True, state="online")
                    except c.NasError as e:
                        out.update(state="offline", error=str(e))
                addresses = [i["ip"] for i in result["interfaces"] if i["name"] == cfg["interface"]]
                out["url"] = f"smb://{addresses[0]}/{share['name']}" if addresses else ""
                result["shares"].append(out)
        except (c.NasError, ValueError, OSError, KeyError) as e:
            result["errors"].append(str(e))
    return result


def demo_snapshot():
    return {"version": c.VERSION, "hostname": "mac-mini-lab", "demo": True, "time": time.time(),
            "configured": True, "service_active": True, "user": "charles", "network": "192.168.1.0/24",
            "interface": "enp0s20u1", "errors": [], "interfaces": [{"name": "enp0s20u1", "ip": "192.168.1.24", "network": "192.168.1.0/24"}],
            "shares": [{"name": "family", "fs": "apfs", "size": 12_000_000_000_000, "mode": "ro", "state": "online", "enabled": True,
                        "online": True, "url": "smb://192.168.1.24/family", "volume": 0}],
            "devices": [
                {"path": "/dev/sda2", "disk": "/dev/sda", "type": "part", "fs": "ext4", "label": "Omarchy", "model": "APPLE HDD", "size": 1_000_000_000_000,
                 "eligible": False, "reason": "系统盘或含活动系统/交换分区", "transport": "sata", "mounts": ["/"]},
                {"path": "/dev/sdc2", "disk": "/dev/sdc", "type": "part", "fs": "apfs", "label": "Family Archive", "model": "MAIWO USB Storage", "size": 12_000_000_000_000,
                 "eligible": True, "reason": "", "transport": "usb", "mounts": ["/srv/omarchy-nas/family"]}
            ]}


def make_handler(demo=False):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            port = self.server.server_port
            # Exact Host validation prevents DNS rebinding. No CORS, writable APIs, or file routes.
            allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
            if self.headers.get("Host") not in allowed or self.headers.get("Sec-Fetch-Site") == "cross-site":
                self.send_error(403)
                return
            routes = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8"),
                      "/Geist-Variable.woff2": ("Geist-Variable.woff2", "font/woff2"),
                      "/GeistMono-Variable.woff2": ("GeistMono-Variable.woff2", "font/woff2")}
            if self.path == "/api/status":
                with self.server.cache_lock:
                    if time.monotonic() - self.server.cache_at > 3:
                        self.server.cache = demo_snapshot() if demo else snapshot()
                        self.server.cache_at = time.monotonic()
                    body = json.dumps(self.server.cache, ensure_ascii=False).encode()
                content_type = "application/json; charset=utf-8"
            elif self.path in routes:
                filename, content_type = routes[self.path]
                body = importlib.resources.files("nas").joinpath("assets", filename).read_bytes()
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)
    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, addr, handler):
        self.cache_lock = threading.Lock()
        self.cache = None
        self.cache_at = 0
        super().__init__(addr, handler)


def serve(port=8787, demo=False, open_browser=True):
    if os.geteuid() == 0 and not demo:
        raise c.NasError("管理页请用普通用户运行，不要加 sudo。")
    if not 1024 <= port <= 65535:
        raise c.NasError("端口须在 1024–65535 之间")
    server = Server(("127.0.0.1", port), make_handler(demo))
    url = f"http://127.0.0.1:{port}"
    print(f"Omarchy NAS {'演示 · 虚构设备' if demo else '本机状态页'}：{url}", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    finally:
        server.server_close()
