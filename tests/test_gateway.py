import contextlib
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import ssl
import tempfile
import threading
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
        def stat_with_mount(path, *args, **kwargs):
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
        self.cfg["network"] = "192.168.1.0/24"
        status, _, body = self.request("GET", "/v1/status", headers={"X-Forwarded-For": "192.168.1.2", "X-Real-IP": "192.168.1.2"})
        self.assertEqual((status, json.loads(body)["error"]), (403, "not_home"))

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
        self.assertEqual(self.request("GET", route, auth=False)[0], 403)

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

    def test_free_space_reserve_and_nested_file(self):
        (self.root / "folder").mkdir()
        self.files.upload("family", "folder/ok", io.BytesIO(b"ok"), 2)
        self.assertEqual((self.root / "folder/ok").read_bytes(), b"ok")
        with patch.object(g.os, "fstatvfs", return_value=types.SimpleNamespace(f_bavail=1, f_frsize=4096)):
            with self.assertRaises(g.ApiError) as raised:
                self.files.upload("family", "full", io.BytesIO(b"a"), 1)
            self.assertEqual(raised.exception.code, "disk_full")


class SetupTests(unittest.TestCase):
    def test_only_exact_https_origins(self):
        self.assertEqual(setup.origin("https://fd.example.test/"), ORIGIN)
        for bad in ("http://fd.test", "https://fd.test/taos", "https://user@fd.test", "https://fd.test?x=1", "https://fd.test/#x"):
            with self.assertRaises(c.NasError): setup.origin(bad)

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL unavailable")
    def test_actual_certificate_generation_trust_and_key_permissions(self):
        # Use an isolated scratch directory; no system services/config touched.
        with tempfile.TemporaryDirectory() as root:
            paths = types.SimpleNamespace(etc=Path(root))
            # Host ownership is tested elsewhere; here simulate installation
            # ownership so this crypto/transport test also runs as non-root.
            def make_test_dir(path, mode):
                path.mkdir(mode=mode, parents=True, exist_ok=True)
            with patch.object(setup.c, "P", paths), patch.object(setup.c, "secure_dir", side_effect=make_test_dir), patch.object(setup.os, "chown"):
                setup.certificates("127.0.0.1", {"gid": os.getgid()})
            self.assertEqual((Path(root) / "tls/ca.key").stat().st_mode & 0o777, 0o600)
            self.assertEqual((Path(root) / "tls/server.key").stat().st_mode & 0o777, 0o640)
            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.load_cert_chain(Path(root) / "tls/server.crt", Path(root) / "tls/server.key")
            client_context = ssl.create_default_context(cafile=str(Path(root) / "family-nas-ca.crt"))
            cfg = {"bind": "127.0.0.1", "port": 0, "network": "127.0.0.0/8", "hosts": [],
                   "origins": [ORIGIN], "familyId": FAMILY, "gatewayId": "test-tls", "shares": [],
                   "maxUploadBytes": g.MAX_UPLOAD, "tokenHash": hashlib.sha256(TOKEN.encode()).hexdigest()}
            server = g.GatewayServer(cfg)
            server.tls_context = server_context
            port = server.server_address[1]
            cfg["hosts"] = [f"127.0.0.1:{port}"]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            conn = http.client.HTTPSConnection("127.0.0.1", port, context=client_context, timeout=5)
            try:
                conn.request("GET", "/v1/status", headers={"Authorization": f"Bearer {TOKEN}", "X-FD-Family": FAMILY, "Origin": ORIGIN})
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertTrue(json.loads(response.read())["homeNetwork"])
            finally:
                conn.close(); server.shutdown(); server.server_close(); thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
