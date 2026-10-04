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
import signal
import socket
import ssl
import stat
import sys
import threading
import time
from urllib.parse import parse_qs, quote, urlsplit

from . import core as c

API_VERSION = 1
MAX_UPLOAD = 2 * 1024**3
STAGING = ".fd-upload-"
CHUNK = 256 * 1024
RESERVE = 64 * 1024**2
LIST_LIMIT, RAW_LIMIT = 10000, 100000
MAX_CONNECTIONS, MAX_PER_PEER = 64, 16
HEADER_DEADLINE = 20
DRAIN_DEADLINE = 600
RENAME_NOREPLACE = 1
FAT = {"exfat", "vfat"}
FULL = {errno.ENOSPC, errno.EDQUOT, errno.EFBIG}


class ApiError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(code)


def allowed_peer(address, network):
    try:
        return ipaddress.ip_address(address) in ipaddress.ip_network(network)
    except ValueError:
        return False


def number(value):
    # ASCII only: str.isdigit() also accepts '²' and other Unicode digits.
    return int(value) if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 15 else None


def path_parts(path):
    # FUSE (apfs-fuse, ntfs-3g) allows 1024-byte names, so navigation accepts them.
    try:
        size = len(path.encode("utf-8"))
    except (AttributeError, UnicodeEncodeError):
        raise ApiError(400, "invalid_path") from None
    if size > 4096 or "\\" in path or "\x00" in path:
        raise ApiError(400, "invalid_path")
    if path == "":
        return []
    parts = path.split("/")
    if any(p in {"", ".", ".."} or p.startswith(STAGING) or len(p.encode("utf-8")) > 1024 for p in parts):
        raise ApiError(400, "invalid_path")
    return parts


def name_problem(name, fs):
    """Why an upload may not create this name on fs: None, "invalid_path" or "invalid_name"."""
    if not isinstance(name, str) or name in {"", ".", ".."} or any(x in name for x in "/\\\x00") or name.startswith(STAGING):
        return "invalid_path"
    try:
        size = len(name.encode("utf-8"))
    except UnicodeEncodeError:
        return "invalid_path"
    # ext4/xfs limit bytes; exFAT/FAT/NTFS limit UTF-16 code units.
    if fs in FAT | {"ntfs"}:
        size = len(name.encode("utf-16-le")) // 2
    if size > 255:
        return "invalid_name"
    # The exFAT/FAT drivers refuse these at commit time with EINVAL.
    if fs in FAT and (any(ch in '"*:<>?|' or ch < " " for ch in name) or name[-1] in " ."):
        return "invalid_name"
    return None


def writable(share):
    return share["mode"] == "rw" and share["fs"] != "apfs"


def share_names(gateway):
    return [s["name"] if isinstance(s, dict) else s for s in gateway["shares"]]


@contextlib.contextmanager
def storage_errors():
    try:
        yield
    except OSError as e:
        if e.errno in FULL:
            raise ApiError(507, "disk_full") from e
        if e.errno == errno.ENAMETOOLONG:
            raise ApiError(400, "invalid_name") from e
        raise


def renameat2(parent_fd, source, target):
    func = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if func is None:
        return errno.ENOSYS
    func.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    func.restype = ctypes.c_int
    return ctypes.get_errno() if func(parent_fd, os.fsencode(source), parent_fd, os.fsencode(target), RENAME_NOREPLACE) else 0


def ntfs_key(name):
    # NTFS compares names through a 1:1 upcase table, so 'ß' never equals 'ss' (unlike casefold()).
    return "".join(u if len(u := ch.upper()) == 1 else ch for ch in name)


def rename_new(parent_fd, source, target, fs=None):
    # Never fall back to an overwriting rename. renameat2(RENAME_NOREPLACE) works
    # without hard links (FAT/exFAT); ntfs-3g's FUSE protocol has no rename flags,
    # so NTFS and other filesystems refusing the flag commit with linkat, which
    # fails with EEXIST rather than replacing.
    if fs != "ntfs":
        code = renameat2(parent_fd, source, target)
        if not code:
            return
        if code == errno.EEXIST:
            raise ApiError(409, "already_exists")
        if code == errno.EINVAL and fs in FAT:
            raise ApiError(400, "invalid_name")
        if code not in {errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP}:
            raise OSError(code, os.strerror(code))
    try:
        os.link(source, target, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
    except FileExistsError:
        raise ApiError(409, "already_exists") from None
    except OSError as e:
        if e.errno in {errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS}:
            raise ApiError(501, "atomic_upload_unsupported") from e
        raise
    with contextlib.suppress(OSError):
        os.unlink(source, dir_fd=parent_fd)


class Files:
    def __init__(self, gateway, p=c.P):
        self.gateway, self.p = gateway, p
        self.lock = threading.Lock()
        self.reserved = {}
        # Staging name -> dup of its parent fd, so stop() can remove what a killed upload leaves.
        self.active = {}
        self.stopping = threading.Event()

    def share(self, name):
        paired = next((s for s in self.gateway["shares"] if s == name or isinstance(s, dict) and s.get("name") == name), None)
        if paired is None:
            raise ApiError(403, "share_denied")
        cfg = c.load_config(self.p)
        s = next((s for s in cfg["shares"] if s["name"] == name and s["enabled"]), None)
        if not s:
            raise ApiError(404, "share_offline")
        # A name re-enrolled for another volume needs a new pairing.
        if isinstance(paired, dict) and any(paired.get(k) != s.get(k) for k in ("id_type", "id", "fs")):
            raise ApiError(403, "share_changed")
        try:
            device = c.match_device(s, c.inventory(p=self.p))
            c.verify_mount(s, device, self.p)
        except c.NasError as e:
            raise ApiError(503, "share_offline") from e
        mode = paired["mode"] if isinstance(paired, dict) else s["mode"]
        return dict(s, mode="rw" if mode == "rw" and writable(s) else "ro")

    @contextlib.contextmanager
    def directory(self, name, parts, *, write=False, share=None):
        s = share or self.share(name)
        if write and not writable(s):
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
        entries, hidden = [], 0
        with self.directory(name, parts) as (fd, s):
            names = os.listdir(fd)
            if len(names) > RAW_LIMIT:
                raise ApiError(413, "directory_too_large")
            for entry in names:
                if entry.startswith(STAGING):
                    continue
                # Omit (and count) names the API could not address, e.g. non-UTF-8 or backslash.
                try:
                    path_parts(f"{path}/{entry}" if path else entry)
                except ApiError:
                    hidden += 1
                    continue
                try:
                    info = os.stat(entry, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                except PermissionError:
                    raise ApiError(403, "access_denied") from None
                except OSError:
                    hidden += 1
                    continue
                if stat.S_ISDIR(info.st_mode):
                    if not os.access(entry, os.R_OK | os.X_OK, dir_fd=fd, follow_symlinks=False):
                        # ext4's root-only lost+found at the volume root holds no family files.
                        if parts or entry != "lost+found":
                            hidden += 1
                        continue
                elif not stat.S_ISREG(info.st_mode):
                    continue
                entries.append({"name": entry, "kind": "directory" if stat.S_ISDIR(info.st_mode) else "file",
                                "size": info.st_size, "modified": info.st_mtime})
            entries.sort(key=lambda x: (x["kind"] != "directory", x["name"].casefold()))
            return {"path": path, "writable": writable(s) and os.access(f"/proc/self/fd/{fd}", os.W_OK),
                    "entries": entries[:LIST_LIMIT], "hidden": hidden, "truncated": len(entries) > LIST_LIMIT, "total": len(entries)}

    @contextlib.contextmanager
    def target(self, name, path, length):
        # Every check that needs no request body, so errors arrive before the upload streams.
        parts = path_parts(path)
        if not parts:
            raise ApiError(400, "invalid_path")
        s = self.share(name)
        problem = name_problem(parts[-1], s["fs"])
        if problem:
            raise ApiError(400, problem)
        if length is None:
            raise ApiError(400, "invalid_request")
        if not 0 <= length <= self.gateway["maxUploadBytes"]:
            raise ApiError(413, "file_too_large")
        with self.directory(name, parts[:-1], write=True, share=s) as (parent, s):
            if not os.access(".", os.W_OK | os.X_OK, dir_fd=parent):
                raise ApiError(403, "permission_denied")
            with storage_errors():
                try:
                    os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise ApiError(409, "already_exists")
                # ntfs-3g is case-sensitive, but FD (and Windows) treat case variants as the same name.
                if s["fs"] == "ntfs":
                    key = ntfs_key(parts[-1])
                    if any(ntfs_key(n) == key for n in os.listdir(parent)):
                        raise ApiError(409, "already_exists")
            yield parent, parts[-1], s

    def reserve(self, parent, length):
        # Concurrent uploads share one free-space budget per filesystem.
        free, dev = os.fstatvfs(parent), os.fstat(parent).st_dev
        with self.lock:
            if free.f_bavail * free.f_frsize - self.reserved.get(dev, 0) < length + RESERVE:
                raise ApiError(507, "disk_full")
            self.reserved[dev] = self.reserved.get(dev, 0) + length
        return dev

    def release(self, dev, length):
        with self.lock:
            self.reserved[dev] = self.reserved.get(dev, 0) - length
            if not self.reserved[dev]:
                del self.reserved[dev]

    def check_upload(self, name, path, length):
        with self.target(name, path, length) as (parent, _name, _s):
            self.release(self.reserve(parent, length), length)

    def stage(self, parent):
        staging, held = STAGING + secrets.token_hex(16), os.dup(parent)
        try:
            with self.lock:
                # Created and registered under the lock: abandon() either sees it or no upload starts after it.
                if self.stopping.is_set():
                    raise ApiError(503, "shutting_down")
                fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
                self.active[staging] = held
        except BaseException:
            os.close(held)
            raise
        return staging, fd

    def abandon(self):
        # Uploads still in fsync/commit when the stop grace ends die with the process. Only staging
        # names are removed; a committed upload no longer has one (or keeps its target link on NTFS).
        with self.lock:
            for staging, parent in self.active.items():
                with contextlib.suppress(OSError):
                    os.unlink(staging, dir_fd=parent)
                os.close(parent)
            self.active.clear()

    def upload(self, name, path, source, length):
        with self.target(name, path, length) as (parent, filename, s), storage_errors():
            dev = self.reserve(parent, length)
            remaining = length
            try:
                staging, fd = self.stage(parent)
                committed = False
                try:
                    with os.fdopen(fd, "wb") as out:
                        while remaining:
                            chunk = b"" if self.stopping.is_set() else source.read(min(remaining, CHUNK))
                            if not chunk:
                                raise ApiError(503, "shutting_down") if self.stopping.is_set() else ApiError(400, "incomplete_upload")
                            out.write(chunk)
                            remaining -= len(chunk)
                            self.release(dev, len(chunk))
                        out.flush()
                        os.fsync(out.fileno())
                    rename_new(parent, staging, filename, s["fs"])
                    committed = True
                    # Some non-POSIX filesystems reject fsync on directories.
                    with contextlib.suppress(OSError):
                        os.fsync(parent)
                finally:
                    if not committed:
                        with contextlib.suppress(FileNotFoundError):
                            os.unlink(staging, dir_fd=parent)
                    with self.lock:
                        held = self.active.pop(staging, None)
                    if held is not None:
                        os.close(held)
            finally:
                self.release(dev, remaining)
        return {"name": filename, "size": length}

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


class Body:
    """Request body limited to its declared length, counting what is left unread."""
    def __init__(self, raw, length):
        self.raw, self.left = raw, length

    def read(self, size):
        chunk = self.raw.read(min(size, self.left))
        self.left -= len(chunk)
        return chunk

    def drain(self, stopping, connection):
        # A wall-clock limit: each wait is capped by the time left and read1 does at most one recv,
        # so a slow drip cannot hold the connection past the deadline.
        deadline = time.monotonic() + DRAIN_DEADLINE
        while self.left and not stopping.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            connection.settimeout(min(45, remaining))
            chunk = self.raw.read1(min(CHUNK, self.left))
            if not chunk:
                break
            self.left -= len(chunk)


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, config, p=c.P, *, files=None, address=None):
        self.config = config
        self.files = files or Files(config, p)
        self.p = p
        self.tickets = {}
        self.ticket_lock = threading.Lock()
        self.upload_slots = threading.BoundedSemaphore(2)
        self.peers = {}
        self.connections = set()
        self.peer_lock = threading.Condition()
        super().__init__(address or (config["bind"], config["port"]), Handler)

    def verify_request(self, request, client_address):
        # Close foreign or excess sockets before any thread, TLS handshake or HTTP parsing.
        ip = client_address[0]
        if not allowed_peer(ip, self.config["network"]):
            return False
        with self.peer_lock:
            if len(self.peers) >= MAX_CONNECTIONS or list(self.peers.values()).count(ip) >= MAX_PER_PEER:
                return False
            self.peers[request] = ip
        return True

    def shutdown_request(self, request):
        try:
            super().shutdown_request(request)
        finally:
            with self.peer_lock:
                self.peers.pop(request, None)
                self.peer_lock.notify_all()

    def get_request(self):
        try:
            return super().get_request()
        except OSError as e:
            # Out of descriptors: back off instead of spinning on a readable listener.
            if e.errno in {errno.EMFILE, errno.ENFILE}:
                time.sleep(0.1)
            raise

    def handle_error(self, request, client_address):
        # TLS failures, resets and timeouts from any peer are routine; keep tracebacks for bugs.
        if isinstance(sys.exc_info()[1], OSError):
            return
        super().handle_error(request, client_address)

    def track(self, connection, active):
        with self.peer_lock:
            (self.connections.add if active else self.connections.discard)(connection)

    def stop(self, grace):
        # Unblock readers so in-flight uploads end and remove their staging files. Known limit:
        # OpenSSL treats this EOF as fatal, so TLS clients see a reset, not the 503 shutting_down.
        self.files.stopping.set()
        with self.peer_lock:
            for connection in self.connections:
                with contextlib.suppress(OSError):
                    socket.socket.shutdown(connection, socket.SHUT_RD)
            self.peer_lock.wait_for(lambda: not self.peers, grace)
        self.files.abandon()

    def run(self, grace=20):
        # systemd stop/restart sends SIGTERM; the default action would skip upload cleanup.
        signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=self.shutdown, daemon=True).start())
        try:
            self.serve_forever()
        finally:
            self.stop(grace)
            self.server_close()

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
        self.server.track(self.connection, True)

    def end_headers(self):
        self.response_started = True
        super().end_headers()

    def finish(self):
        self.server.track(self.connection, False)
        try:
            super().finish()
        finally:
            # TLS wrapping detaches the original socket owned by socketserver.
            # Close the replacement explicitly rather than relying on GC.
            if isinstance(self.connection, ssl.SSLSocket):
                self.connection.close()

    def handle_one_request(self):
        # The 45 s timeout is per recv; a slow drip of header bytes must not hold a thread.
        self.expired = False
        self.watchdog = threading.Timer(HEADER_DEADLINE, self.expire)
        self.watchdog.daemon = True
        self.watchdog.start()
        try:
            super().handle_one_request()
        finally:
            self.watchdog.cancel()

    def expire(self):
        self.expired = True
        with contextlib.suppress(OSError):
            socket.socket.shutdown(self.connection, socket.SHUT_RDWR)

    def parse_request(self):
        ok = super().parse_request()
        self.watchdog.cancel()
        if self.expired:
            self.close_connection = True
            return False
        return ok

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
            self.authorized = True

    def headers_for(self, status, length, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "sandbox allow-downloads; default-src 'none'")
        if self.close_connection:
            self.send_header("Connection", "close")
        origin = self.headers.get("Origin")
        if origin in self.server.config["origins"]:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.headers_for(status, len(body), "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def no_content(self):
        # Downloads are top-level navigations; a 204 keeps the FD tab instead of showing JSON.
        self.send_response(204)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

    def route(self):
        try:
            parsed = urlsplit(self.path)
        except ValueError:
            raise ApiError(400, "invalid_request") from None
        def arg(name):
            # Parsed lazily so that check() runs before any query handling.
            try:
                values = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=6).get(name)
            except ValueError:
                raise ApiError(400, "invalid_request") from None
            if not values or len(values) != 1:
                raise ApiError(400, "invalid_request")
            return values[0]
        return parsed.path, arg

    def protect(self, fn):
        self.response_started = False
        self.body, self.quiet, self.denied = None, False, "access_denied"
        def fail(status, code):
            self.close_connection = True
            if self.response_started:
                return
            with contextlib.suppress(OSError):
                if self.quiet:
                    self.no_content()
                else:
                    self.json(status, {"error": code})
                # Browsers report a reset, not the response, if the unread upload is cut off.
                if self.body and code != "shutting_down":
                    self.body.drain(self.server.files.stopping, self.connection)
        try:
            fn()
        except ApiError as e:
            fail(e.status, e.code)
        except FileNotFoundError:
            fail(404, "not_found")
        except PermissionError:
            fail(403, self.denied)
        except (ConnectionError, TimeoutError, ssl.SSLError):
            self.close_connection = True
        except OSError as e:
            # A file, symlink or over-long name inside the path: no such directory is offered.
            if e.errno in {errno.ENOTDIR, errno.ELOOP, errno.ENAMETOOLONG}:
                fail(404, "not_found")
            else:
                fail(503, "storage_unavailable")
        except (ValueError, c.NasError):
            fail(503, "storage_unavailable")

    def do_OPTIONS(self):
        # Out-of-CIDR peers are closed in verify_request before TLS (DoS hardening), so a
        # browser outside the home network sees a network error, never a readable not_home.
        def action():
            self.check(preflight=True)
            requested = {x.strip().lower() for x in self.headers.get("Access-Control-Request-Headers", "").split(",") if x.strip()}
            if not requested.issubset({"authorization", "content-type", "x-fd-family"}):
                raise ApiError(403, "header_denied")
            if self.headers.get("Access-Control-Request-Method") not in {"GET", "POST", "PUT"}:
                raise ApiError(403, "method_denied")
            # A preflight needs no token: never let one keep a connection slot alive by itself.
            if not getattr(self, "authorized", False):
                self.close_connection = True
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
            self.quiet = route == "/v1/download"
            self.check(ticket=self.quiet)
            if route == "/v1/status":
                shares = []
                for name in share_names(self.server.config):
                    try:
                        s = self.server.files.share(name)
                        shares.append({"name": name, "online": True, "writable": writable(s), "fs": s["fs"]})
                    except ApiError as e:
                        shares.append({"name": name, "online": False, "writable": False, "fs": "", "reason": e.code})
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
                        chunk = os.read(fd, min(remaining, CHUNK))
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
            if route == "/v1/download-ticket":
                name, path = arg("share"), arg("path")
                with self.server.files.file(name, path):
                    pass
                value = self.server.ticket(self.client_address[0], name, path)
                self.json(200, {"ticket": value, "expiresIn": 60})
            elif route == "/v1/upload-check":
                # Advisory: PUT repeats every check and stays authoritative.
                self.denied = "permission_denied"
                self.server.files.check_upload(arg("share"), arg("path"), number(arg("size")))
                if not self.server.upload_slots.acquire(blocking=False):
                    raise ApiError(429, "busy")
                self.server.upload_slots.release()
                self.json(200, {"ok": True})
            else:
                raise ApiError(404, "not_found")
        self.protect(action)

    def do_PUT(self):
        def action():
            self.denied = "permission_denied"
            # Only an authorized upload is drained for a readable error; others close at once.
            self.check()
            lengths = self.headers.get_all("Content-Length", [])
            length = None if self.headers.get("Transfer-Encoding") or len(lengths) != 1 else number(lengths[0])
            if length is not None and length <= self.server.config["maxUploadBytes"]:
                self.body = Body(self.rfile, length)
            route, arg = self.route()
            if route != "/v1/file":
                raise ApiError(404, "not_found")
            if length is None:
                raise ApiError(411, "length_required")
            if self.headers.get_content_type() != "application/octet-stream":
                raise ApiError(415, "invalid_content_type")
            if not self.server.upload_slots.acquire(blocking=False):
                raise ApiError(429, "busy")
            try:
                result = self.server.files.upload(arg("share"), arg("path"), self.body or self.rfile, length)
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
    server.run()
