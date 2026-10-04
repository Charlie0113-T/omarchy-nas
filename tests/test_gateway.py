import contextlib
import ctypes
import errno
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import select
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from nas import core as c
from nas import gateway as g
from nas import gateway_setup as setup

TOKEN = "t" * 43
FAMILY = "728942b4-eed1-43ab-bab4-78301f64cdaa"
ORIGIN = "https://fd.example.test"
PAIRED = {"name": "family", "id_type": "uuid", "id": "usb-1", "fs": "ext4", "mode": "rw"}
OCTET = {"Content-Type": "application/octet-stream"}

# Runs the real serve loop in a child process with the same mocks as GatewayTests.
LAUNCHER = r"""
import json, os, sys, time, types
from pathlib import Path
from unittest.mock import patch
from nas import gateway as g
mounts, cfg, grace, delay = Path(sys.argv[1]), json.loads(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
real_fsync = os.fsync
def slow_fsync(fd):
    # Stands in for a slow USB disk writing back a large upload.
    time.sleep(delay)
    real_fsync(fd)
patch.object(g.os, "fsync", side_effect=slow_fsync).start()
files = g.Files(cfg, types.SimpleNamespace(mounts=mounts))
patch.object(files, "share", return_value={"mode": "rw", "fs": "ext4"}).start()
real_stat = os.stat
def stat_with_mount(path, *args, **kwargs):
    result = real_stat(path, *args, **kwargs)
    if path == mounts:
        values = list(result)
        values[2] = result.st_dev + 100000
        return os.stat_result(values)
    return result
patch.object(g.os, "stat", side_effect=stat_with_mount).start()
server = g.GatewayServer(cfg, files=files)
cfg["hosts"] = [f"127.0.0.1:{server.server_address[1]}"]
print(server.server_address[1], flush=True)
server.run(grace=grace)
"""


class CountingReader(io.BytesIO):
    consumed = 0
    def read(self, size=-1):
        chunk = super().read(size)
        self.consumed += len(chunk)
        return chunk


def read_all(sock):
    data = b""
    while chunk := sock.recv(65536):
        data += chunk
    return data


def put_head(port, path, length, auth=True):
    lines = [f"PUT /v1/file?share=family&path={path} HTTP/1.1", f"Host: 127.0.0.1:{port}", f"Origin: {ORIGIN}",
             "Content-Type: application/octet-stream", f"Content-Length: {length}"]
    if auth:
        lines += [f"Authorization: Bearer {TOKEN}", f"X-FD-Family: {FAMILY}"]
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


class NameRuleTests(unittest.TestCase):
    def test_upload_names_follow_the_target_filesystem(self):
        long_cjk = "家" * 90 + ".txt"  # 274 UTF-8 bytes, 94 UTF-16 units
        cases = {("What?.mov", "exfat"): "invalid_name", ("What?.mov", "ntfs"): None, ("What?.mov", "ext4"): None,
                 ('a "b".txt', "vfat"): "invalid_name", ("trail.", "vfat"): "invalid_name", ("trail ", "exfat"): "invalid_name",
                 ("tab\t", "exfat"): "invalid_name", (long_cjk, "exfat"): None, (long_cjk, "ntfs"): None,
                 (long_cjk, "ext4"): "invalid_name", ("😀" * 128, "exfat"): "invalid_name", ("a" * 255, "xfs"): None,
                 ("a" * 256, "xfs"): "invalid_name", ("a\\b", "ext4"): "invalid_path", ("..", "ext4"): "invalid_path",
                 (g.STAGING + "x", "ext4"): "invalid_path", ("caf\udce9", "ext4"): "invalid_path",
                 ("", "ext4"): "invalid_path", ("a/b", "ext4"): "invalid_path"}
        for (name, fs), expected in cases.items():
            self.assertEqual(g.name_problem(name, fs), expected, (name, fs))
        self.assertEqual(g.path_parts("dir/" + long_cjk), ["dir", long_cjk])
        self.assertEqual(g.path_parts("a" * 1024), ["a" * 1024])
        for bad in ("a" * 1025, "x/" * 2048 + "x", "caf\udce9", "a/../b", "a\\b"):
            with self.assertRaises(g.ApiError):
                g.path_parts(bad)


class ShareIdentityTests(unittest.TestCase):
    def setUp(self):
        for name in ("inventory", "match_device", "verify_mount"):
            patch.object(g.c, name).start()
        self.addCleanup(patch.stopall)

    def files(self, paired, **live):
        share = {"name": "family", "id_type": "uuid", "id": "usb-1", "fs": "ext4", "mode": "rw", "enabled": True, **live}
        patch.object(g.c, "load_config", return_value={"shares": [share]}).start()
        return g.Files({"shares": paired, "maxUploadBytes": 1024}, types.SimpleNamespace())

    def error(self, files, name="family"):
        with self.assertRaises(g.ApiError) as raised:
            files.share(name)
        return raised.exception.status, raised.exception.code

    def test_paired_identity_and_mode_bind_the_live_share(self):
        self.assertEqual(self.files([PAIRED]).share("family")["mode"], "rw")
        for change in ({"id": "other-disk"}, {"id_type": "partuuid"}, {"fs": "exfat"}):
            self.assertEqual(self.error(self.files([PAIRED], **change)), (403, "share_changed"))
        files = self.files([dict(PAIRED, mode="ro")])
        self.assertEqual(files.share("family")["mode"], "ro")
        with self.assertRaises(g.ApiError) as raised:
            files.upload("family", "x.txt", io.BytesIO(b"x"), 1)
        self.assertEqual(raised.exception.code, "read_only")
        self.assertEqual(self.files([dict(PAIRED, fs="apfs")], fs="apfs", mode="rw").share("family")["mode"], "ro")
        self.assertEqual(self.error(self.files([PAIRED], enabled=False)), (404, "share_offline"))
        self.assertEqual(self.error(self.files([PAIRED]), "photos"), (403, "share_denied"))

    def test_legacy_name_list_uses_live_mode(self):
        self.assertEqual(self.files(["family"]).share("family")["mode"], "rw")
        self.assertEqual(self.files(["family"], mode="ro").share("family")["mode"], "ro")
        self.assertEqual(self.error(self.files(["photos"])), (403, "share_denied"))


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mounts = Path(self.tmp.name) / "mounts"
        self.root = self.mounts / "family"
        self.root.mkdir(parents=True)
        self.cfg = {"bind": "127.0.0.1", "port": 0, "network": "127.0.0.0/8", "hosts": [],
                    "origins": [ORIGIN], "familyId": FAMILY, "gatewayId": "gateway-test",
                    "tokenHash": hashlib.sha256(TOKEN.encode()).hexdigest(), "shares": ["family"], "maxUploadBytes": 1024**2}
        self.share = {"mode": "rw", "fs": "ext4"}
        self.files = g.Files(self.cfg, types.SimpleNamespace(mounts=self.mounts))
        # The container has no USB mount. Mock only mount identity; traversal,
        # file descriptors, atomic rename and HTTP below use the real OS.
        self.share_mock = patch.object(self.files, "share", return_value=self.share).start()
        self.addCleanup(patch.stopall)
        real_stat = os.stat
        self.stat_errors = {}
        def stat_with_mount(path, *args, **kwargs):
            if isinstance(path, str) and path in self.stat_errors:
                raise self.stat_errors[path]
            result = real_stat(path, *args, **kwargs)
            if path == self.mounts:
                values = list(result)
                values[2] = result.st_dev + 100000
                return os.stat_result(values)
            return result
        self.stat_patch = patch.object(g.os, "stat", side_effect=stat_with_mount)
        self.stat_patch.start()
        self.server = g.GatewayServer(self.cfg, files=self.files)
        self.port = self.server.server_address[1]
        self.cfg["hosts"] = [f"127.0.0.1:{self.port}"]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def request(self, method, route, *, body=None, headers=None, auth=True):
        values = {"Origin": ORIGIN}
        if auth:
            values.update(Authorization=f"Bearer {TOKEN}", **{"X-FD-Family": FAMILY})
        values.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        try:
            conn.request(method, route, body=body, headers=values)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def test_authenticated_home_status_and_private_network_preflight(self):
        status, headers, body = self.request("GET", "/v1/status")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["homeNetwork"])
        self.assertEqual(json.loads(body)["clientIp"], "127.0.0.1")
        self.assertEqual(headers["Access-Control-Allow-Origin"], ORIGIN)
        status, headers, _ = self.request("OPTIONS", "/v1/file", auth=False, headers={
            "Access-Control-Request-Method": "PUT", "Access-Control-Request-Headers": "authorization,content-type,x-fd-family",
            "Access-Control-Request-Private-Network": "true"})
        self.assertEqual(status, 204)
        self.assertEqual(headers["Access-Control-Allow-Private-Network"], "true")

    def test_auth_family_origin_host_and_real_peer_are_enforced(self):
        self.assertEqual(self.request("GET", "/v1/status", auth=False)[0], 401)
        for headers in ({"Authorization": "Bearer wrong"}, {"X-FD-Family": "other"}, {"Origin": "https://evil.test"}, {"Host": "evil.test"}):
            self.assertIn(self.request("GET", "/v1/status", headers=headers)[0], (401, 403))
        # Foreign peers are closed before HTTP is parsed; forwarded-IP headers cannot help.
        self.cfg["network"] = "192.168.1.0/24"
        with self.assertRaises(ConnectionError):
            self.request("GET", "/v1/status", headers={"X-Forwarded-For": "192.168.1.2", "X-Real-IP": "192.168.1.2"})

    def test_round_trip_upload_list_and_one_use_download(self):
        payload = bytes(range(256)) * 512
        query = urlencode({"share": "family", "path": "中文 空格.bin"})
        self.assertEqual(self.request("PUT", "/v1/file?" + query, body=payload, headers={"Content-Type": "application/octet-stream"})[0], 201)
        status, _, body = self.request("GET", "/v1/list?share=family&path=")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["entries"][0]["name"], "中文 空格.bin")
        status, _, body = self.request("POST", "/v1/download-ticket?" + query)
        self.assertEqual(status, 200)
        route = "/v1/download?ticket=" + json.loads(body)["ticket"]
        status, headers, body = self.request("GET", route, auth=False)
        self.assertEqual(status, 200)
        self.assertEqual(body, payload)
        self.assertTrue(headers["Content-Disposition"].startswith("attachment;"))
        self.assertEqual(self.request("GET", route, auth=False)[::2], (204, b""))

    def test_upload_requires_auth_and_never_overwrites(self):
        (self.root / "keep.txt").write_bytes(b"original")
        route = "/v1/file?share=family&path=keep.txt"
        headers = {"Content-Type": "application/octet-stream"}
        self.assertEqual(self.request("PUT", route, auth=False, body=b"attack", headers=headers)[0], 401)
        self.assertEqual(self.request("PUT", route, body=b"replace", headers=headers)[0], 409)
        self.assertEqual((self.root / "keep.txt").read_bytes(), b"original")
        self.assertEqual(list(self.root.glob(g.STAGING + "*")), [])

    def test_readonly_and_apfs_rejected_server_side(self):
        for fs, mode in (("apfs", "rw"), ("ext4", "ro")):
            self.share.update(fs=fs, mode=mode)
            with self.assertRaises(g.ApiError) as raised:
                self.files.upload("family", "no.txt", io.BytesIO(b"no"), 2)
            self.assertEqual(raised.exception.code, "read_only")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_traversal_and_symlinks_cannot_escape(self):
        secret = Path(self.tmp.name) / "secret"
        secret.write_bytes(b"outside")
        (self.root / "link").symlink_to(secret)
        (self.root / "dirlink").symlink_to(secret.parent, target_is_directory=True)
        for path in ("../secret", "/etc/passwd", "a/../../secret", "a\\secret", ".fd-upload-temp"):
            with self.assertRaises(g.ApiError):
                self.files.listing("family", path)
        for path in ("link", "dirlink/secret"):
            with self.assertRaises(OSError):
                with self.files.file("family", path):
                    self.fail("symlink opened")
        self.assertEqual(self.files.listing("family", "")["entries"], [])
        self.assertEqual(secret.read_bytes(), b"outside")

    def test_interrupted_and_oversize_uploads_leave_no_file(self):
        with self.assertRaises(g.ApiError) as raised:
            self.files.upload("family", "partial", io.BytesIO(b"incomplete"), 1000)
        self.assertEqual(raised.exception.code, "incomplete_upload")
        with self.assertRaises(g.ApiError) as raised:
            self.files.upload("family", "large", io.BytesIO(), self.cfg["maxUploadBytes"] + 1)
        self.assertEqual(raised.exception.code, "file_too_large")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_empty_mount_directory_is_not_a_share(self):
        self.stat_patch.stop()
        with self.assertRaises(g.ApiError) as raised:
            self.files.listing("family", "")
        self.assertEqual(raised.exception.code, "share_offline")

    def test_download_ticket_expires_and_is_bound_to_peer(self):
        token = self.server.ticket("192.168.1.10", "family", "a")
        with self.assertRaises(g.ApiError): self.server.redeem(token, "192.168.1.11")
        token = self.server.ticket("192.168.1.10", "family", "a")
        with patch.object(g.time, "monotonic", return_value=10**20):
            with self.assertRaises(g.ApiError): self.server.redeem(token, "192.168.1.10")

    def test_status_accepts_paired_share_objects(self):
        self.cfg["shares"] = [PAIRED]
        status, _, body = self.request("GET", "/v1/status")
        self.assertEqual((status, json.loads(body)["shares"]), (200, [{"name": "family", "online": True, "writable": True, "fs": "ext4"}]))

    def test_status_says_why_a_share_is_offline(self):
        # The real Files.share; only the config and the USB device lookup are mocked.
        self.share_mock.side_effect = lambda name: g.Files.share(self.files, name)
        for name in ("inventory", "match_device", "verify_mount"):
            patch.object(g.c, name).start()
        live = dict(PAIRED, enabled=True)
        patch.object(g.c, "load_config", return_value={"shares": [live]}).start()
        self.cfg["shares"] = [PAIRED]
        def shares():
            status, _, body = self.request("GET", "/v1/status")
            self.assertEqual(status, 200)
            return json.loads(body)["shares"]
        self.assertEqual(shares(), [{"name": "family", "online": True, "writable": True, "fs": "ext4"}])
        offline = {"name": "family", "online": False, "writable": False, "fs": ""}
        live["id"] = "other-disk"
        self.assertEqual(shares(), [dict(offline, reason="share_changed")])
        live.update(id="usb-1", enabled=False)
        self.assertEqual(shares(), [dict(offline, reason="share_offline")])
        live["enabled"] = True
        g.c.verify_mount.side_effect = c.NasError("not mounted")
        self.assertEqual(shares(), [dict(offline, reason="share_offline")])

    def test_commit_without_rename_flags_links_and_never_overwrites(self):
        calls = []
        def renameat2(parent, _source, _parent, target, flags):
            calls.append(flags)
            try:
                os.stat(target, dir_fd=parent, follow_symlinks=False)
                ctypes.set_errno(errno.EEXIST)
            except FileNotFoundError:
                ctypes.set_errno(errno.EINVAL)  # FUSE without rename2, as with ntfs-3g
            return -1
        race = self.root / "race.bin"
        class Racer(io.BytesIO):
            # An SMB client creates the name after the pre-check, before commit.
            def read(self, size=-1):
                if not race.exists():
                    race.write_bytes(b"smb")
                return super().read(size)
        never = {"side_effect": AssertionError("overwriting rename")}
        with patch.object(g.ctypes, "CDLL", return_value=types.SimpleNamespace(renameat2=renameat2)), \
                patch.object(g.os, "rename", **never), patch.object(g.os, "replace", **never):
            for fs in ("ntfs", "xfs"):
                self.share["fs"] = fs
                payload = os.urandom(300000)
                self.assertEqual(self.request("PUT", f"/v1/file?share=family&path={fs}.bin", body=payload, headers=OCTET)[0], 201)
                self.assertEqual((self.root / f"{fs}.bin").read_bytes(), payload)
                with self.assertRaises(g.ApiError) as raised:
                    self.files.upload("family", "race.bin", Racer(b"upload"), 6)
                self.assertEqual(raised.exception.code, "already_exists")
                self.assertEqual(race.read_bytes(), b"smb")
                race.unlink()
            self.share["fs"] = "exfat"
            status, _, body = self.request("PUT", "/v1/file?share=family&path=new.bin", body=b"x", headers=OCTET)
            self.assertEqual((status, json.loads(body)["error"]), (400, "invalid_name"))
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["ntfs.bin", "xfs.bin"])
        self.assertTrue(calls)
        self.assertNotIn(0, calls)
        self.assertEqual((self.files.reserved, self.files.active), ({}, {}))

    def test_bad_names_and_existing_targets_fail_before_body(self):
        reader = CountingReader(b"x" * 1000)
        self.share["fs"] = "exfat"
        for name in ("What?.mov", 'Invoice "Oct".pdf', "trail.", "ctl\x01"):
            with self.assertRaises(g.ApiError) as raised:
                self.files.upload("family", name, reader, 1000)
            self.assertEqual(raised.exception.code, "invalid_name")
        self.share["fs"] = "ext4"
        (self.root / "IMG.mov").write_bytes(b"orig")
        with patch.object(g.os, "fstatvfs", return_value=types.SimpleNamespace(f_bavail=0, f_frsize=4096)):
            with self.assertRaises(g.ApiError) as raised:
                self.files.upload("family", "IMG.mov", reader, 1000)
        self.assertEqual(raised.exception.code, "already_exists")
        self.assertEqual(reader.consumed, 0)
        self.assertEqual([p.name for p in self.root.iterdir()], ["IMG.mov"])
        self.assertEqual((self.root / "IMG.mov").read_bytes(), b"orig")

    def test_ntfs_existence_check_ignores_case(self):
        (self.root / "report.pdf").write_bytes(b"orig")
        reader = CountingReader(b"x" * 10)
        self.share["fs"] = "ntfs"
        for name in ("Report.PDF", "REPORT.pdf"):
            with self.assertRaises(g.ApiError) as raised:
                self.files.check_upload("family", name, 10)
            self.assertEqual(raised.exception.code, "already_exists")
            status, _, body = self.request("PUT", "/v1/file?" + urlencode({"share": "family", "path": name}), body=b"x" * 10, headers=OCTET)
            self.assertEqual((status, json.loads(body)["error"]), (409, "already_exists"))
        with self.assertRaises(g.ApiError) as raised:
            self.files.upload("family", "Report.PDF", reader, 10)
        self.assertEqual((raised.exception.code, reader.consumed), ("already_exists", 0))
        self.files.check_upload("family", "report2.pdf", 10)
        self.share["fs"] = "ext4"
        self.files.check_upload("family", "Report.PDF", 10)
        self.assertEqual([p.name for p in self.root.iterdir()], ["report.pdf"])

    def test_listing_hides_unaddressable_and_failing_entries(self):
        (self.root / "ok.txt").write_bytes(b"ok")
        (self.root / "a\\b.txt").write_bytes(b"x")
        os.close(os.open(os.path.join(os.fsencode(self.root), "照片.jpg".encode("gbk")), os.O_CREAT | os.O_WRONLY))
        (self.root / "bad.docx").write_bytes(b"x")
        (self.root / (g.STAGING + "abc")).write_bytes(b"partial")
        (self.root / "link").symlink_to(self.root / "ok.txt")
        os.mkfifo(self.root / "fifo")
        (self.root / "lost+found").mkdir()
        (self.root / "private").mkdir()
        (self.root / "sub/lost+found").mkdir(parents=True)
        self.stat_errors["bad.docx"] = OSError(errno.EIO, "I/O error")
        real_access = os.access
        def access(path, mode, **kwargs):
            return path not in {"lost+found", "private"} and real_access(path, mode, **kwargs)
        with patch.object(g.os, "access", side_effect=access):
            status, _, body = self.request("GET", "/v1/list?share=family&path=")
            sub = self.files.listing("family", "sub")
        listing = json.loads(body)
        self.assertEqual((status, [e["name"] for e in listing["entries"]]), (200, ["sub", "ok.txt"]))
        # The root's lost+found is skipped silently; 'private' and a nested lost+found still count.
        self.assertEqual((listing["hidden"], listing["truncated"], listing["total"], listing["writable"]), (4, False, 2, True))
        self.assertEqual((sub["entries"], sub["hidden"]), ([], 1))
        self.stat_errors["ok.txt"] = PermissionError(errno.EACCES, "denied")
        status, _, body = self.request("GET", "/v1/list?share=family&path=")
        self.assertEqual((status, json.loads(body)["error"]), (403, "access_denied"))

    def test_fresh_ext4_root_reports_nothing_hidden(self):
        # mkfs.ext4 leaves lost+found root:root 0700 even with -E root_owner.
        (self.root / "lost+found").mkdir(mode=0o700)
        real_access = os.access
        with patch.object(g.os, "access", side_effect=lambda path, mode, **kw: path != "lost+found" and real_access(path, mode, **kw)):
            listing = self.files.listing("family", "")
        self.assertEqual((listing["entries"], listing["hidden"], listing["total"]), ([], 0, 0))

    def test_read_and_write_permission_errors_are_distinct(self):
        (self.root / "locked").mkdir()
        real_open, real_access = os.open, os.access
        def open_denied(path, *args, **kwargs):
            if path == "locked":
                raise PermissionError(errno.EACCES, "denied")
            return real_open(path, *args, **kwargs)
        with patch.object(g.os, "open", side_effect=open_denied):
            status, _, body = self.request("GET", "/v1/list?share=family&path=locked")
        self.assertEqual((status, json.loads(body)["error"]), (403, "access_denied"))
        def no_write(path, mode, **kwargs):
            return not (path == "." and mode & os.W_OK) and real_access(path, mode, **kwargs)
        with patch.object(g.os, "access", side_effect=no_write):
            for method, route in (("PUT", "/v1/file?share=family&path=locked/x"), ("POST", "/v1/upload-check?share=family&path=locked/x&size=1")):
                status, _, body = self.request(method, route, body=b"x" if method == "PUT" else None, headers=OCTET)
                self.assertEqual((status, json.loads(body)["error"]), (403, "permission_denied"))
        self.assertEqual(list((self.root / "locked").iterdir()), [])

    def test_large_directory_is_truncated_after_filtering(self):
        folder = self.root / "Photos"
        folder.mkdir()
        for i in range(g.LIST_LIMIT + 1):
            os.close(os.open(folder / f"{i:05}.jpg", os.O_CREAT | os.O_WRONLY))
        for i in range(5):
            (folder / f"{g.STAGING}{i}").write_bytes(b"")
        status, _, body = self.request("GET", "/v1/list?share=family&path=Photos")
        listing = json.loads(body)
        self.assertEqual((status, len(listing["entries"]), listing["truncated"], listing["total"], listing["hidden"]),
                         (200, g.LIST_LIMIT, True, g.LIST_LIMIT + 1, 0))
        self.assertEqual(listing["entries"][0]["name"], "00000.jpg")
        with patch.object(g.os, "listdir", return_value=["x"] * (g.RAW_LIMIT + 1)):
            with self.assertRaises(g.ApiError) as raised:
                self.files.listing("family", "Photos")
        self.assertEqual(raised.exception.code, "directory_too_large")

    def test_early_put_errors_are_readable_after_draining_body(self):
        self.cfg["maxUploadBytes"] = 16 * 1024**2
        payload = b"x" * (8 * 1024**2)
        def put(path="big.bin", **headers):
            status, response, body = self.request("PUT", "/v1/file?" + urlencode({"share": "family", "path": path}),
                                                  body=payload, headers={**OCTET, **headers})
            return status, response.get("Connection"), json.loads(body)["error"]
        with patch.object(g.os, "fstatvfs", return_value=types.SimpleNamespace(f_bavail=0, f_frsize=4096)):
            self.assertEqual(put(), (507, "close", "disk_full"))
        for _ in range(2):
            self.server.upload_slots.acquire()
        try:
            self.assertEqual(put(), (429, "close", "busy"))
        finally:
            for _ in range(2):
                self.server.upload_slots.release()
        self.share["fs"] = "exfat"
        self.assertEqual(put("What?.mov"), (400, "close", "invalid_name"))
        self.share.update(fs="apfs", mode="ro")
        self.assertEqual(put(), (403, "close", "read_only"))
        self.assertEqual(list(self.root.iterdir()), [])

    def wait_for_no_peers(self):
        deadline = time.monotonic() + 3
        while self.server.peers and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.server.peers, {})

    def test_drain_deadline_is_wall_clock(self):
        self.cfg["maxUploadBytes"] = 16 * 1024**2
        (self.root / "taken").write_bytes(b"x")
        with patch.object(g, "DRAIN_DEADLINE", 1), socket.create_connection(("127.0.0.1", self.port), timeout=3) as sock:
            sock.sendall(put_head(self.port, "taken", 8 * 1024**2))
            self.assertTrue(sock.recv(65536).startswith(b"HTTP/1.1 409 "))
            # One byte at a time never fills a 256 KiB read; the deadline must still end the drain.
            start, closed = time.monotonic(), False
            while not closed and time.monotonic() - start < 4:
                try:
                    sock.send(b"z")
                    if select.select([sock], [], [], 0.2)[0]:
                        closed = sock.recv(65536) == b""
                except OSError:
                    closed = True
            self.assertTrue(closed)
            self.assertLess(time.monotonic() - start, 2.5)
        self.wait_for_no_peers()
        self.assertEqual((self.root / "taken").read_bytes(), b"x")

    def test_unauthenticated_put_is_not_drained(self):
        self.cfg["maxUploadBytes"] = 16 * 1024**2
        with socket.create_connection(("127.0.0.1", self.port), timeout=1) as sock:
            sock.sendall(put_head(self.port, "x.bin", 8 * 1024**2, auth=False))
            # No body is sent: the server must answer and close without waiting for one.
            response = read_all(sock)
        self.assertTrue(response.startswith(b"HTTP/1.1 401 "))
        self.assertIn(b'"unauthorized"', response)
        self.wait_for_no_peers()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_preflight_alone_cannot_keep_a_connection(self):
        preflight = (f"OPTIONS /v1/file HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nOrigin: {ORIGIN}\r\n"
                     "Access-Control-Request-Method: PUT\r\nAccess-Control-Request-Headers: authorization,content-type,x-fd-family\r\n\r\n").encode()
        with socket.create_connection(("127.0.0.1", self.port), timeout=2) as sock:
            sock.sendall(preflight)
            response = read_all(sock)  # the server must close after one tokenless preflight
        self.assertTrue(response.startswith(b"HTTP/1.1 204 "))
        self.assertIn(b"Connection: close", response)
        self.wait_for_no_peers()
        # A connection that already passed the full check keeps keep-alive for later preflights.
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        try:
            conn.request("GET", "/v1/status", headers={"Origin": ORIGIN, "Authorization": f"Bearer {TOKEN}", "X-FD-Family": FAMILY})
            conn.getresponse().read()
            conn.request("OPTIONS", "/v1/file", headers={"Origin": ORIGIN, "Access-Control-Request-Method": "PUT"})
            response = conn.getresponse()
            response.read()
            self.assertEqual((response.status, response.getheader("Connection")), (204, None))
        finally:
            conn.close()

    def test_ntfs_name_key_matches_windows_not_casefold(self):
        self.share["fs"] = "ntfs"
        (self.root / "straße.txt").write_bytes(b"x")
        (self.root / "ﬁle.pdf").write_bytes(b"x")
        self.files.check_upload("family", "strasse.txt", 10)
        self.files.check_upload("family", "file.pdf", 10)
        with self.assertRaises(g.ApiError) as raised:
            self.files.check_upload("family", "STRAßE.TXT", 10)
        self.assertEqual(raised.exception.code, "already_exists")

    def test_upload_check_reports_each_pre_body_error_without_writing(self):
        (self.root / "dir").mkdir()
        (self.root / "dir/taken.txt").write_bytes(b"x")
        def check(path="dir/new.txt", size="10", **share):
            self.share.update({"mode": "rw", "fs": "ext4", **share})
            status, headers, body = self.request("POST", "/v1/upload-check?" + urlencode({"share": "family", "path": path, "size": size}))
            self.assertEqual(headers["Access-Control-Allow-Origin"], ORIGIN)
            body = json.loads(body)
            return status, body.get("error", body)
        self.assertEqual(check(), (200, {"ok": True}))
        for kwargs, expected in (({"path": ""}, (400, "invalid_path")), ({"path": "dir/a\\b"}, (400, "invalid_path")),
                                 ({"path": "dir/What?.mov", "fs": "exfat"}, (400, "invalid_name")),
                                 ({"size": "1e3"}, (400, "invalid_request")), ({"size": "²"}, (400, "invalid_request")),
                                 ({"size": str(2 * 1024**2)}, (413, "file_too_large")),
                                 ({"fs": "apfs", "mode": "ro"}, (403, "read_only")), ({"mode": "ro"}, (403, "read_only")),
                                 ({"path": "missing/new.txt"}, (404, "not_found")), ({"path": "dir/taken.txt/x"}, (404, "not_found")),
                                 ({"path": "dir/taken.txt"}, (409, "already_exists"))):
            self.assertEqual(check(**kwargs), expected, kwargs)
        with patch.object(g.os, "fstatvfs", return_value=types.SimpleNamespace(f_bavail=0, f_frsize=4096)):
            self.assertEqual(check(), (507, "disk_full"))
        for _ in range(2):
            self.server.upload_slots.acquire()
        try:
            self.assertEqual(check(), (429, "busy"))
        finally:
            for _ in range(2):
                self.server.upload_slots.release()
        status, _, body = self.request("POST", "/v1/upload-check?share=family&path=dir/new.txt")
        self.assertEqual((status, json.loads(body)["error"]), (400, "invalid_request"))
        self.assertEqual(sorted(p.name for p in self.root.rglob("*")), ["dir", "taken.txt"])

    def test_download_failures_answer_no_content(self):
        (self.root / "a.txt").write_bytes(b"a")
        for route, headers in (("/v1/download?ticket=" + "x" * 43, {}), ("/v1/download?ticket=", {}), ("/v1/download", {}),
                               ("/v1/download?ticket=x", {"Host": "evil.test"})):
            status, response, body = self.request("GET", route, auth=False, headers=headers)
            self.assertEqual((status, body), (204, b""), route)
            self.assertNotIn("Content-Type", response)
        status, _, body = self.request("POST", "/v1/download-ticket?share=family&path=a.txt")
        route = "/v1/download?ticket=" + json.loads(body)["ticket"]
        (self.root / "a.txt").unlink()
        self.assertEqual(self.request("GET", route, auth=False)[::2], (204, b""))

    def test_write_errors_map_to_disk_full_and_reservations_are_shared(self):
        with patch.object(g.os, "fsync", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            status, _, body = self.request("PUT", "/v1/file?share=family&path=full.bin", body=b"x" * 1000, headers=OCTET)
        self.assertEqual((status, json.loads(body)["error"]), (507, "disk_full"))
        self.assertEqual(list(self.root.iterdir()), [])
        self.cfg["maxUploadBytes"] = 512 * 1024**2
        started, release, errors = threading.Event(), threading.Event(), []
        class Slow:
            def read(self, size):
                if not started.is_set():
                    started.set()
                    return b"x" * size
                release.wait(5)
                return b""
        def first():
            try:
                self.files.upload("family", "a.bin", Slow(), 200 * 1024**2)
            except g.ApiError as e:
                errors.append(e.code)
        with patch.object(g.os, "fstatvfs", return_value=types.SimpleNamespace(f_bavail=300 * 256, f_frsize=4096)):
            thread = threading.Thread(target=first)
            thread.start()
            self.assertTrue(started.wait(5))
            with self.assertRaises(g.ApiError) as raised:
                self.files.upload("family", "b.bin", io.BytesIO(), 200 * 1024**2)
            self.assertEqual(raised.exception.code, "disk_full")
            status, _, body = self.request("POST", f"/v1/upload-check?share=family&path=b.bin&size={200 * 1024**2}")
            self.assertEqual((status, json.loads(body)["error"]), (507, "disk_full"))
            release.set()
            thread.join(5)
        self.assertEqual((errors, self.files.reserved), (["incomplete_upload"], {}))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_foreign_and_excess_connections_are_closed_before_a_thread(self):
        self.cfg["network"] = "192.168.1.0/24"
        before = threading.active_count()
        with socket.create_connection(("127.0.0.1", self.port), timeout=2) as sock:
            self.assertEqual(sock.recv(1), b"")
        self.assertLessEqual(threading.active_count(), before)
        self.cfg["network"] = "127.0.0.0/8"
        with patch.object(g, "MAX_PER_PEER", 2):
            idle = [socket.create_connection(("127.0.0.1", self.port), timeout=2) for _ in range(2)]
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=2) as sock:
                    self.assertEqual(sock.recv(1), b"")
            finally:
                for sock in idle:
                    sock.close()
            deadline = time.monotonic() + 3
            while self.server.peers and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(self.request("GET", "/v1/status")[0], 200)

    def test_slow_header_drip_is_cut_off(self):
        with patch.object(g, "HEADER_DEADLINE", 0.5), socket.create_connection(("127.0.0.1", self.port), timeout=3) as sock:
            sock.sendall(b"GET /v1/status HTTP/1.1\r\nX-Drip: ")
            start, closed = time.monotonic(), False
            while not closed and time.monotonic() - start < 3:
                try:
                    sock.send(b"a")
                    if select.select([sock], [], [], 0.2)[0]:
                        closed = sock.recv(1024) == b""
                except OSError:
                    closed = True
            self.assertTrue(closed)
            self.assertLess(time.monotonic() - start, 2)

    def test_malformed_length_and_query_are_client_errors(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        try:
            conn.putrequest("PUT", "/v1/file?share=family&path=x", skip_accept_encoding=True)
            for key, value in {"Origin": ORIGIN, "Authorization": f"Bearer {TOKEN}", "X-FD-Family": FAMILY,
                               **OCTET, "Content-Length": "²"}.items():
                conn.putheader(key, value)
            conn.endheaders()
            response = conn.getresponse()
            self.assertEqual((response.status, json.loads(response.read())["error"]), (411, "length_required"))
        finally:
            conn.close()
        route = "/v1/list?" + "&".join(f"f{i}=1" for i in range(7))
        for auth, expected in ((True, (400, "invalid_request")), (False, (401, "unauthorized"))):
            status, _, body = self.request("GET", route, auth=auth)
            self.assertEqual((status, json.loads(body)["error"]), expected)

    def test_tls_and_reset_failures_log_no_traceback(self):
        def handshake(*_args, **_kwargs):
            raise ssl.SSLError(1, "[SSL: HTTP_REQUEST] http request")
        self.server.tls_context = types.SimpleNamespace(wrap_socket=handshake)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with socket.create_connection(("127.0.0.1", self.port), timeout=2) as sock:
                sock.sendall(b"GET /v1/status HTTP/1.1\r\n\r\n")
                with contextlib.suppress(ConnectionError):
                    self.assertEqual(sock.recv(1), b"")
            try:
                raise KeyError("unexpected")
            except KeyError:
                self.server.handle_error(None, ("127.0.0.1", 1))
        self.server.tls_context = None
        self.assertEqual(stderr.getvalue().count("Traceback"), 1)
        self.assertIn("KeyError", stderr.getvalue())
        self.assertEqual(self.request("GET", "/v1/status")[0], 200)

    def launch(self, grace=5, fsync_delay=0):
        cfg = dict(self.cfg, maxUploadBytes=8 * 1024**2)
        proc = subprocess.Popen([sys.executable, "-c", LAUNCHER, str(self.mounts), json.dumps(cfg), str(grace), str(fsync_delay)],
                                text=True, cwd=Path(g.__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(proc.stderr.close)
        self.addCleanup(proc.stdout.close)
        self.addCleanup(proc.kill)
        return proc, int(proc.stdout.readline())

    def wait_for_staging(self, size):
        deadline = time.monotonic() + 5
        while not any(p.stat().st_size >= size for p in self.root.glob(g.STAGING + "*")) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(list(self.root.glob(g.STAGING + "*")))

    def test_sigterm_during_plain_http_upload_answers_503_and_removes_staging(self):
        # Plain HTTP only: over TLS the stop() read shutdown kills the session and the client sees a reset.
        proc, port = self.launch()
        with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
            sock.sendall(put_head(port, "movie.mov", 4 * 1024**2) + b"x" * 1024**2)
            self.wait_for_staging(1)
            proc.send_signal(signal.SIGTERM)
            self.assertEqual(proc.wait(timeout=15), 0, proc.stderr.read())
            response = read_all(sock)
        self.assertIn(b" 503 ", response.split(b"\r\n")[0])
        self.assertIn(b'"shutting_down"', response)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_sigterm_during_fsync_leaves_no_staging(self):
        # The whole body has arrived and the disk is still writing it back when the stop grace ends.
        proc, port = self.launch(grace=0.5, fsync_delay=3)
        with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
            sock.sendall(put_head(port, "movie.mov", 1024**2) + b"x" * 1024**2)
            self.wait_for_staging(1024**2)
            proc.send_signal(signal.SIGTERM)
            self.assertEqual(proc.wait(timeout=15), 0, proc.stderr.read())
        self.assertEqual(list(self.root.glob(g.STAGING + "*")), [])
        self.assertIn([p.name for p in self.root.iterdir()], ([], ["movie.mov"]))

    def test_free_space_reserve_and_nested_file(self):
        (self.root / "folder").mkdir()
        self.files.upload("family", "folder/ok", io.BytesIO(b"ok"), 2)
        self.assertEqual((self.root / "folder/ok").read_bytes(), b"ok")
        with patch.object(g.os, "fstatvfs", return_value=types.SimpleNamespace(f_bavail=1, f_frsize=4096)):
            with self.assertRaises(g.ApiError) as raised:
                self.files.upload("family", "full", io.BytesIO(b"a"), 1)
            self.assertEqual(raised.exception.code, "disk_full")


if __name__ == "__main__":
    unittest.main()
