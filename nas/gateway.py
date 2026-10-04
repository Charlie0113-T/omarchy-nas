"""Authenticated, LAN-only HTTPS file gateway for Family Dashboard.

Runs as the same ordinary user as SMB. No privileged HTTP operations.
Files never pass through Vercel or Supabase.
"""
from __future__ import annotations

import contextlib
import ctypes
import errno
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import stat
import threading
import time
from urllib.parse import parse_qs, quote, urlsplit

from . import core as c

API_VERSION = 1
MAX_UPLOAD = 2 * 1024**3
STAGING = ".fd-upload-"


class ApiError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(code)


def allowed_peer(address, network):
    try:
        return ipaddress.ip_address(address) in ipaddress.ip_network(network)
    except ValueError:
        return False


def path_parts(path):
    if not isinstance(path, str) or len(path.encode("utf-8")) > 4096 or "\\" in path or "\x00" in path:
        raise ApiError(400, "invalid_path")
    if path == "":
        return []
    parts = path.split("/")
    if any(p in {"", ".", ".."} or p.startswith(STAGING) or len(p.encode("utf-8")) > 255 for p in parts):
        raise ApiError(400, "invalid_path")
    return parts


def rename_new(parent_fd, source, target):
    # Linux renameat2 provides atomic NOREPLACE on filesystems without hardlinks
    # (including FAT/exFAT). Never fall back to an overwriting rename.
    libc = ctypes.CDLL(None, use_errno=True)
    func = getattr(libc, "renameat2", None)
    if func is None:
        raise ApiError(501, "atomic_upload_unsupported")
    func.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    func.restype = ctypes.c_int
    if func(parent_fd, os.fsencode(source), parent_fd, os.fsencode(target), 1):
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise ApiError(409, "already_exists")
        raise OSError(code, os.strerror(code))


class Files:
    def __init__(self, gateway, p=c.P):
        self.gateway, self.p = gateway, p

    def share(self, name):
        if name not in self.gateway["shares"]:
            raise ApiError(403, "share_denied")
        cfg = c.load_config(self.p)
        s = next((s for s in cfg["shares"] if s["name"] == name and s["enabled"]), None)
        if not s:
            raise ApiError(404, "share_offline")
        try:
            device = c.match_device(s, c.inventory(p=self.p))
            c.verify_mount(s, device, self.p)
        except c.NasError as e:
            raise ApiError(503, "share_offline") from e
        return s

    @contextlib.contextmanager
    def directory(self, name, parts, *, write=False):
        s = self.share(name)
        if write and (s["mode"] != "rw" or s["fs"] == "apfs"):
            raise ApiError(403, "read_only")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(self.p.mounts / c.valid_name(name), flags)
        try:
            # Recheck after opening, protecting against a disappearing mount.
            self.share(name)
            dev = os.fstat(fd).st_dev
            current = os.stat(self.p.mounts / name, follow_symlinks=False)
            if dev == os.stat(self.p.mounts).st_dev or (dev, os.fstat(fd).st_ino) != (current.st_dev, current.st_ino):
                raise ApiError(503, "share_offline")
            for part in parts:
                child = os.open(part, flags, dir_fd=fd)
                if os.fstat(child).st_dev != dev:
                    os.close(child)
                    raise ApiError(403, "nested_mount_denied")
                os.close(fd)
                fd = child
            yield fd, s
        finally:
            os.close(fd)

    def listing(self, name, path):
        parts = path_parts(path)
        entries = []
        with self.directory(name, parts) as (fd, s):
            names = os.listdir(fd)
            if len(names) > 10000:
                raise ApiError(413, "directory_too_large")
            for entry in names:
                if entry.startswith(STAGING):
                    continue
                try:
                    info = os.stat(entry, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    continue
                entries.append({"name": entry, "kind": "directory" if stat.S_ISDIR(info.st_mode) else "file",
                                "size": info.st_size, "modified": info.st_mtime})
            entries.sort(key=lambda x: (x["kind"] != "directory", x["name"].casefold()))
            return {"path": path, "entries": entries, "writable": s["mode"] == "rw" and os.access(f"/proc/self/fd/{fd}", os.W_OK)}

    def upload(self, name, path, source, length):
        parts = path_parts(path)
        if not parts:
            raise ApiError(400, "invalid_path")
        if not 0 <= length <= self.gateway["maxUploadBytes"]:
            raise ApiError(413, "file_too_large")
        with self.directory(name, parts[:-1], write=True) as (parent, _s):
            free = os.fstatvfs(parent)
            if free.f_bavail * free.f_frsize < length + 64 * 1024**2:
                raise ApiError(507, "disk_full")
            staging = STAGING + secrets.token_hex(16)
            fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
            committed = False
            try:
                with os.fdopen(fd, "wb") as out:
                    remaining = length
                    while remaining:
                        chunk = source.read(min(remaining, 1024 * 256))
                        if not chunk:
                            raise ApiError(400, "incomplete_upload")
                        out.write(chunk)
                        remaining -= len(chunk)
                    out.flush()
                    os.fsync(out.fileno())
                rename_new(parent, staging, parts[-1])
                committed = True
                # Some non-POSIX filesystems reject fsync on directories.
                with contextlib.suppress(OSError):
                    os.fsync(parent)
            finally:
                if not committed:
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(staging, dir_fd=parent)
        return {"name": parts[-1], "size": length}

    @contextlib.contextmanager
    def file(self, name, path):
        parts = path_parts(path)
        if not parts:
            raise ApiError(400, "invalid_path")
        with self.directory(name, parts[:-1]) as (parent, _s):
            # O_NONBLOCK avoids blocking if a file has been swapped for a FIFO.
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_dev != os.fstat(parent).st_dev:
                    raise ApiError(403, "file_type_denied")
                yield fd, info, parts[-1]
            finally:
                os.close(fd)


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, config, p=c.P, *, files=None, address=None):
        self.config = config
        self.files = files or Files(config, p)
        self.p = p
        self.tickets = {}
        self.ticket_lock = threading.Lock()
        self.upload_slots = threading.BoundedSemaphore(2)
        super().__init__(address or (config["bind"], config["port"]), Handler)

    def ticket(self, peer, share, path):
        with self.ticket_lock:
            now = time.monotonic()
            self.tickets = {k: v for k, v in self.tickets.items() if v[0] > now}
            if len(self.tickets) >= 128:
                raise ApiError(429, "busy")
            value = secrets.token_urlsafe(32)
            self.tickets[value] = (now + 60, peer, share, path)
            return value

    def redeem(self, value, peer):
        with self.ticket_lock:
            item = self.tickets.pop(value, None)
        if not item or item[0] < time.monotonic() or item[1] != peer:
            raise ApiError(403, "download_expired")
        return item[2:]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FamilyNAS"

    def setup(self):
        if getattr(self.server, "tls_context", None):
            self.request.settimeout(10)
            # Do the handshake in this request's thread, never in the accept loop.
            self.request = self.server.tls_context.wrap_socket(self.request, server_side=True)
        super().setup()
        self.connection.settimeout(45)

    def end_headers(self):
        self.response_started = True
        super().end_headers()

    def finish(self):
        try:
            super().finish()
        finally:
            # TLS wrapping detaches the original socket owned by socketserver.
            # Close the replacement explicitly rather than relying on GC.
            if isinstance(self.connection, ssl.SSLSocket):
                self.connection.close()

    def log_message(self, *_args):
        # Never put bearer tokens, filenames or one-time download tickets in logs.
        pass

    def check(self, *, ticket=False, preflight=False):
        cfg = self.server.config
        if not allowed_peer(self.client_address[0], cfg["network"]):
            raise ApiError(403, "not_home")
        if self.headers.get("Host") not in cfg["hosts"]:
            raise ApiError(403, "host_denied")
        origin = self.headers.get("Origin")
        if origin is not None and origin not in cfg["origins"]:
            raise ApiError(403, "origin_denied")
        if preflight:
            if origin not in cfg["origins"]:
                raise ApiError(403, "origin_denied")
            return
        if not ticket:
            value = self.headers.get("Authorization", "")
            digest = hashlib.sha256(value.removeprefix("Bearer ").encode()).hexdigest()
            if not value.startswith("Bearer ") or not hmac.compare_digest(digest, cfg["tokenHash"]):
                raise ApiError(401, "unauthorized")
            if self.headers.get("X-FD-Family") != cfg["familyId"]:
                raise ApiError(403, "family_mismatch")

    def headers_for(self, status, length, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "sandbox allow-downloads; default-src 'none'")
        origin = self.headers.get("Origin")
        if origin in self.server.config["origins"]:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.headers_for(status, len(body), "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def route(self):
        parsed = urlsplit(self.path)
        params = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=6)
        def arg(name):
            values = params.get(name)
            if not values or len(values) != 1:
                raise ApiError(400, "invalid_request")
            return values[0]
        return parsed.path, arg

    def protect(self, fn):
        self.response_started = False
        def fail(status, code):
            self.close_connection = True
            if not self.response_started:
                with contextlib.suppress(ConnectionError, TimeoutError):
                    self.json(status, {"error": code})
        try:
            fn()
        except ApiError as e:
            fail(e.status, e.code)
        except FileNotFoundError:
            fail(404, "not_found")
        except PermissionError:
            fail(403, "permission_denied")
        except (ConnectionError, TimeoutError, socket.timeout):
            self.close_connection = True
        except (OSError, ValueError, c.NasError):
            fail(503, "storage_unavailable")

    def do_OPTIONS(self):
        def action():
            self.check(preflight=True)
            requested = {x.strip().lower() for x in self.headers.get("Access-Control-Request-Headers", "").split(",") if x.strip()}
            if not requested.issubset({"authorization", "content-type", "x-fd-family"}):
                raise ApiError(403, "header_denied")
            if self.headers.get("Access-Control-Request-Method") not in {"GET", "POST", "PUT"}:
                raise ApiError(403, "method_denied")
            self.headers_for(204, 0, "application/json")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-FD-Family")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Access-Control-Max-Age", "300")
            self.end_headers()
        self.protect(action)

    def do_GET(self):
        def action():
            route, arg = self.route()
            self.check(ticket=route == "/v1/download")
            if route == "/v1/status":
                shares = []
                for name in self.server.config["shares"]:
                    try:
                        s = self.server.files.share(name)
                        shares.append({"name": name, "online": True, "writable": s["mode"] == "rw" and s["fs"] != "apfs", "fs": s["fs"]})
                    except ApiError:
                        shares.append({"name": name, "online": False, "writable": False, "fs": ""})
                self.json(200, {"protocolVersion": API_VERSION, "gatewayId": self.server.config["gatewayId"],
                                "familyId": self.server.config["familyId"], "homeNetwork": True,
                                "clientIp": self.client_address[0], "shares": shares,
                                "maxUploadBytes": self.server.config["maxUploadBytes"]})
            elif route == "/v1/list":
                self.json(200, self.server.files.listing(arg("share"), arg("path")))
            elif route == "/v1/download":
                name, path = self.server.redeem(arg("ticket"), self.client_address[0])
                with self.server.files.file(name, path) as (fd, info, filename):
                    self.headers_for(200, info.st_size, "application/octet-stream")
                    self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(filename, safe=""))
                    self.end_headers()
                    remaining = info.st_size
                    while remaining:
                        chunk = os.read(fd, min(remaining, 1024 * 256))
                        if not chunk:
                            self.close_connection = True
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            else:
                raise ApiError(404, "not_found")
        self.protect(action)

    def do_POST(self):
        def action():
            self.check()
            route, arg = self.route()
            if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Length", "0") != "0":
                raise ApiError(400, "invalid_request")
            if route != "/v1/download-ticket":
                raise ApiError(404, "not_found")
            name, path = arg("share"), arg("path")
            with self.server.files.file(name, path):
                pass
            value = self.server.ticket(self.client_address[0], name, path)
            self.json(200, {"ticket": value, "expiresIn": 60})
        self.protect(action)

    def do_PUT(self):
        def action():
            self.check()
            route, arg = self.route()
            if route != "/v1/file":
                raise ApiError(404, "not_found")
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get("Transfer-Encoding") or len(lengths) != 1 or not lengths[0].isdigit():
                raise ApiError(411, "length_required")
            if self.headers.get_content_type() != "application/octet-stream":
                raise ApiError(415, "invalid_content_type")
            if not self.server.upload_slots.acquire(blocking=False):
                raise ApiError(429, "busy")
            try:
                result = self.server.files.upload(arg("share"), arg("path"), self.rfile, int(lengths[0]))
                self.json(201, result)
            finally:
                self.server.upload_slots.release()
        self.protect(action)


def serve():
    if os.geteuid() == 0:
        raise c.NasError("文件网关必须以普通共享用户运行。")
    cfg = json.loads((c.P.etc / "gateway.json").read_text())
    if not allowed_peer(cfg["bind"], cfg["network"]):
        raise c.NasError("网关绑定地址不在登记的家庭网段中。")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(c.P.etc / "tls/server.crt"), str(c.P.etc / "tls/server.key"))
    server = GatewayServer(cfg)
    server.tls_context = context
    print(f"Family NAS HTTPS gateway: {cfg['bind']}:{cfg['port']}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
