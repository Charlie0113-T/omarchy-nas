import contextlib
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

from nas import core as c
from nas import cli
from nas import gateway as g
from nas import gateway_setup as setup

TOKEN = "t" * 43
FAMILY = "728942b4-eed1-43ab-bab4-78301f64cdaa"
ORIGIN = "https://fd.example.test"


def make_test_dir(path, mode=0o755):
    Path(path).mkdir(mode=mode, parents=True, exist_ok=True)


def nas_config(paused=True):
    return {"schema": 1, "version": c.VERSION, "user": "charles", "uid": 1000, "gid": 1000,
            "interface": "eth0", "network": "192.168.1.0/24", "shares": [
                {"name": "family", "id_type": "uuid", "id": "usb-333", "fs": "apfs", "size": 10**12,
                 "serial": "s1", "mode": "ro", "enabled": True, "volume": 0},
                {"name": "upload", "id_type": "partuuid", "id": "part-444", "fs": "ext4", "size": 10**11,
                 "serial": "s2", "mode": "rw", "enabled": not paused, "volume": 0}]}


def openssl(*args):
    return subprocess.run(["openssl", *map(str, args)], capture_output=True, text=True)


def issue(root, address="127.0.0.1"):
    # Real CA and gateway certificates in a scratch directory; ownership is simulated.
    with patch.object(c, "P", types.SimpleNamespace(etc=Path(root))), patch.object(c, "secure_dir", side_effect=make_test_dir), patch.object(setup.os, "chown"):
        setup.certificates(address, {"gid": os.getgid(), "network": "127.0.0.0/8"})
    return Path(root) / "tls"


def tls_server(cert, key):
    # Another HTTPS program holding the gateway port.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    listener = socket.create_server(("127.0.0.1", 0))
    def accept():
        with contextlib.suppress(OSError):
            while True:
                conn, _ = listener.accept()
                with contextlib.suppress(OSError), conn, context.wrap_socket(conn, server_side=True):
                    pass
    threading.Thread(target=accept, daemon=True).start()
    return listener


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
            with patch.object(setup.c, "P", paths), patch.object(setup.c, "secure_dir", side_effect=make_test_dir), patch.object(setup.os, "chown"):
                setup.certificates("127.0.0.1", {"gid": os.getgid(), "network": "127.0.0.0/8"})
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

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL unavailable")
    def test_ca_is_name_constrained_and_cannot_vouch_for_other_sites(self):
        with tempfile.TemporaryDirectory() as root:
            tls = Path(root) / "tls"
            with patch.object(setup.c, "P", types.SimpleNamespace(etc=Path(root))), patch.object(setup.c, "secure_dir", side_effect=make_test_dir), patch.object(setup.os, "chown"):
                setup.certificates("192.168.1.24", {"gid": os.getgid(), "network": "127.0.0.0/8"})
            ca = openssl("x509", "-in", tls / "ca.crt", "-noout", "-text", "-enddate", "-startdate").stdout
            self.assertIn("X509v3 Name Constraints: critical", ca)
            for subtree in ("IP:10.0.0.0/255.0.0.0", "IP:172.16.0.0/255.240.0.0", "IP:192.168.0.0/255.255.0.0", "IP:127.0.0.0/255.0.0.0", "DNS:invalid"):
                self.assertIn(subtree, ca)
            days = (c.cert_expiry(tls / "ca.crt") - c.cert_expiry(tls / "server.crt")) / 86400
            self.assertAlmostEqual(days, 1825 - 365, delta=1)
            leaf = openssl("x509", "-in", tls / "server.crt", "-noout", "-subject", "-ext", "subjectAltName").stdout
            self.assertRegex(leaf, r"CN ?= ?Family NAS")
            self.assertIn("IP Address:192.168.1.24", leaf)
            self.assertNotIn("DNS:", leaf)
            real = openssl("verify", "-CAfile", tls / "ca.crt", tls / "server.crt")
            self.assertEqual(real.returncode, 0, real.stderr)
            # A stolen ca.key must not mint certificates that family devices would accept for real sites.
            for san, cn in (("DNS:www.example.com", "www.example.com"), ("IP:8.8.8.8", "Family NAS")):
                with self.subTest(san=san):
                    ext = Path(root) / "rogue.ext"; ext.write_text(f"subjectAltName={san}\nbasicConstraints=critical,CA:FALSE\n")
                    self.assertEqual(openssl("req", "-new", "-newkey", "rsa:2048", "-nodes", "-subj", f"/CN={cn}/",
                                             "-keyout", Path(root) / "rogue.key", "-out", Path(root) / "rogue.csr").returncode, 0)
                    self.assertEqual(openssl("x509", "-req", "-in", Path(root) / "rogue.csr", "-CA", tls / "ca.crt", "-CAkey", tls / "ca.key",
                                             "-set_serial", "7", "-days", "30", "-extfile", ext, "-out", Path(root) / "rogue.crt").returncode, 0)
                    rogue = openssl("verify", "-CAfile", tls / "ca.crt", Path(root) / "rogue.crt")
                    self.assertNotEqual(rogue.returncode, 0)
                    self.assertIn("subtree violation", rogue.stdout + rogue.stderr)

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL unavailable")
    def test_renew_reissues_only_the_server_certificate(self):
        with tempfile.TemporaryDirectory() as root:
            etc = Path(root)
            gateway = {"protocolVersion": 1, "gatewayId": "gw-1", "familyId": FAMILY, "bind": "192.168.1.24", "port": 8789,
                       "network": "192.168.1.0/24", "tokenHash": "a" * 64, "shares": [{"name": "family"}]}
            (etc / "gateway.json").write_text(json.dumps(gateway))
            (etc / "pairing.json").write_text('{"token": "keep"}')
            real_run, calls = c.run, []
            def fake_run(args, **kwargs):
                if args[0] == "openssl":
                    return real_run(args, **kwargs)
                calls.append(list(args))
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            with patch.object(setup.c, "P", types.SimpleNamespace(etc=etc)), patch.object(setup.c, "secure_dir", side_effect=make_test_dir), \
                 patch.object(setup.os, "chown"), patch.object(c, "load_config", return_value=nas_config()), patch.object(cli, "root"), \
                 patch.object(c, "run", side_effect=fake_run), patch.object(c, "UFW", etc / "missing-ufw.conf"), \
                 patch.object(setup, "wait_ready") as ready, contextlib.redirect_stdout(io.StringIO()) as out:
                setup.certificates("192.168.1.24", nas_config())
                before = {name: (etc / name).read_bytes() for name in ("gateway.json", "pairing.json", "tls/ca.crt", "tls/ca.key", "family-nas-ca.crt")}
                serials = [openssl("x509", "-in", etc / "tls/server.crt", "-noout", "-serial").stdout]
                for _ in range(2):
                    setup.renew()
                    serials.append(openssl("x509", "-in", etc / "tls/server.crt", "-noout", "-serial").stdout)
            self.assertEqual({name: (etc / name).read_bytes() for name in before}, before)
            self.assertEqual(len(set(serials)), 3)
            self.assertEqual(openssl("verify", "-CAfile", etc / "tls/ca.crt", etc / "tls/server.crt").returncode, 0)
            self.assertEqual(calls, [["systemctl", "is-active", "--quiet", setup.SERVICE], ["systemctl", "restart", setup.SERVICE]] * 2)
            ready.assert_called_with("192.168.1.24", 8789)
            self.assertIn("pairing.json 未改变", out.getvalue())
            self.assertIn("家庭 CA 证书 " + setup.day(c.cert_expiry(etc / "tls/ca.crt")) + " 到期（剩 1824 天）", out.getvalue())
            self.assertIn("port 8789 proto tcp", out.getvalue())

    def test_renew_does_not_start_a_stopped_gateway(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "gateway.json").write_text(json.dumps({"bind": "192.168.1.24", "port": 8789, "network": "192.168.1.0/24"}))
            (Path(root) / "tls").mkdir(); (Path(root) / "tls/ca.key").write_text("k"); (Path(root) / "tls/ca.crt").write_text("c")
            with patch.object(setup.c, "P", types.SimpleNamespace(etc=Path(root))), patch.object(c, "load_config", return_value=nas_config()), \
                 patch.object(cli, "root"), patch.object(setup, "certificates") as certs, patch.object(c, "cert_expiry", return_value=0), \
                 patch.object(c, "run", return_value=types.SimpleNamespace(returncode=3, stdout="", stderr="")) as run, \
                 patch.object(setup, "wait_ready") as ready, patch.object(c, "UFW", Path(root) / "none"), contextlib.redirect_stdout(io.StringIO()) as out:
                setup.renew()
        certs.assert_called_once()
        ready.assert_not_called()
        self.assertEqual([call.args[0][1] for call in run.call_args_list], ["is-active"])
        self.assertIn("sudo omarchy-nas start", out.getvalue())

    def test_renew_never_creates_a_new_ca(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "gateway.json").write_text(json.dumps({"bind": "192.168.1.24", "port": 8789}))
            with patch.object(setup.c, "P", types.SimpleNamespace(etc=Path(root))), patch.object(c, "load_config", return_value=nas_config()), \
                 patch.object(cli, "root"), patch.object(setup, "certificates") as certs, self.assertRaisesRegex(c.NasError, "ca.key") as raised:
                setup.renew()
            certs.assert_not_called()
            for part in ("轮换 CA", "sudo mv -n", "--replace", "family-nas-ca.crt", "pairing.json"):
                self.assertIn(part, str(raised.exception))
            (Path(root) / "gateway.json").unlink()
            with patch.object(setup.c, "P", types.SimpleNamespace(etc=Path(root))), patch.object(c, "load_config", return_value=nas_config()), \
                 patch.object(cli, "root"), self.assertRaisesRegex(c.NasError, "尚未配对"):
                setup.renew()

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL unavailable")
    def test_renewed_leaf_never_outlives_ca(self):
        real_run = c.run
        def short_ca(args, **kwargs):
            # Model the CA's last month: same commands, a 30-day CA.
            return real_run(["30" if arg == "1825" else arg for arg in args], **kwargs)
        with tempfile.TemporaryDirectory() as root, patch.object(c, "run", side_effect=short_ca), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            issue(root)
            tls = issue(root)
            ca, leaf = c.cert_expiry(tls / "ca.crt"), c.cert_expiry(tls / "server.crt")
        self.assertLessEqual(leaf, ca - 86400)
        self.assertAlmostEqual(leaf, time.time() + 28 * 86400, delta=300)
        self.assertIn("剩 29 天", out.getvalue())
        self.assertIn("网关证书只签发 28 天", out.getvalue())
        self.assertIn("轮换 CA", out.getvalue())

    def test_expired_ca_is_not_used_to_sign(self):
        for left, word in ((-86400, "已于"), (1.5 * 86400, "将于")):
            with self.subTest(word=word), tempfile.TemporaryDirectory() as root:
                tls = Path(root) / "tls"; tls.mkdir(); (tls / "ca.key").write_text("k"); (tls / "ca.crt").write_text("c")
                with patch.object(c, "P", types.SimpleNamespace(etc=Path(root))), patch.object(c, "secure_dir", side_effect=make_test_dir), \
                     patch.object(setup.os, "chown"), patch.object(c, "cert_expiry", return_value=time.time() + left) as expiry, \
                     patch.object(c, "run") as run, self.assertRaises(c.NasError) as raised:
                    setup.certificates("192.168.1.24", nas_config())
                expiry.assert_called_once_with(tls / "ca.crt")
                run.assert_not_called()
                for part in (word, "续期无效", f"{tls}/ca.key.old-", f"{tls}/ca.crt.old-", "--replace", "family-nas-ca.crt", "pairing.json"):
                    self.assertIn(part, str(raised.exception))
                self.assertEqual(sorted(p.name for p in tls.iterdir()), ["ca.crt", "ca.key"])
                self.assertFalse((Path(root) / "family-nas-ca.crt").exists())

    def test_gateway_unit_is_sandboxed_without_hiding_devices(self):
        text = setup.unit({"user": "charles", "gid": 1000})
        for line in ("ProtectHome=yes", "ProtectSystem=full", "PrivateTmp=yes", "ProtectKernelTunables=yes", "ProtectKernelModules=yes",
                     "ProtectControlGroups=yes", "RestrictNamespaces=yes", "LockPersonality=yes", "RestrictRealtime=yes",
                     "SystemCallArchitectures=native", "InaccessiblePaths=-/run/docker.sock", "LimitNOFILE=1024", "TasksMax=256",
                     "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK", "NoNewPrivileges=true", "UMask=0077"):
            self.assertIn(line + "\n", text)
        for line in ("PrivateDevices", "ProtectSystem=strict", "SystemCallFilter"):
            self.assertNotIn(line, text)

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL unavailable")
    def test_wait_ready_rejects_foreign_listeners(self):
        # systemd reports the crash-looping gateway active; the journal shows why it cannot bind.
        def fake_run(args, **kwargs):
            out = "OSError: [Errno 98] Address already in use\n\x1b[31m" if args[0] == "journalctl" else ""
            return types.SimpleNamespace(returncode=0, stdout=out, stderr="")
        with tempfile.TemporaryDirectory() as root:
            tls = issue(root)
            for name in ("crt", "key"):
                shutil.copy(tls / f"server.{name}", Path(root) / f"previous.{name}")
            issue(root)
            openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "30", "-subj", "/CN=Other/", "-addext", "subjectAltName=IP:127.0.0.1",
                    "-keyout", Path(root) / "other.key", "-out", Path(root) / "other.crt")
            plain = socket.create_server(("127.0.0.1", 0))
            listeners = {"plain TCP": plain, "foreign TLS": tls_server(Path(root) / "other.crt", Path(root) / "other.key"),
                         "same CA, not this server.crt": tls_server(Path(root) / "previous.crt", Path(root) / "previous.key")}
            try:
                for case, listener in listeners.items():
                    with self.subTest(case=case), patch.object(c, "run", side_effect=fake_run) as run, \
                         self.assertRaisesRegex(c.NasError, "Errno 98") as raised:
                        setup.wait_ready("127.0.0.1", listener.getsockname()[1], timeout=1, tls=tls)
                    self.assertNotIn("\x1b", str(raised.exception))
                    self.assertEqual(run.call_args.args[0][:3], ["journalctl", "-u", setup.SERVICE])
            finally:
                for listener in listeners.values():
                    with contextlib.suppress(OSError):
                        listener.shutdown(socket.SHUT_RDWR)
                    listener.close()

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL unavailable")
    def test_wait_ready_accepts_this_gateway(self):
        with tempfile.TemporaryDirectory() as root:
            tls = issue(root)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(tls / "server.crt", tls / "server.key")
            cfg = {"bind": "127.0.0.1", "port": 0, "network": "127.0.0.0/8", "hosts": [], "origins": [ORIGIN], "familyId": FAMILY,
                   "gatewayId": "ready", "shares": [], "maxUploadBytes": g.MAX_UPLOAD, "tokenHash": "a" * 64}
            server = g.GatewayServer(cfg)
            server.tls_context = context
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                with patch.object(c, "run", return_value=types.SimpleNamespace(returncode=0, stdout="", stderr="")) as run:
                    setup.wait_ready("127.0.0.1", server.server_address[1], timeout=5, tls=tls)
                self.assertIn(["systemctl", "is-active", "--quiet", setup.SERVICE], [call.args[0] for call in run.call_args_list])
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=5)

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL unavailable")
    def test_wait_ready_pins_the_certificate_and_reports_expiry_instead_of_timing_out(self):
        # An expired (or otherwise unverifiable) server.crt still identifies this gateway; upgrade must
        # not call it "not started" but point at gateway-renew.
        with tempfile.TemporaryDirectory() as root:
            tls = issue(root)
            openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "30", "-subj", "/CN=Unchained/", "-addext", "subjectAltName=IP:127.0.0.1",
                    "-keyout", tls / "server.key", "-out", tls / "server.crt")
            listener = tls_server(tls / "server.crt", tls / "server.key")
            expiry = lambda path: time.time() - 86400 if path.name == "server.crt" else time.time() + 86400 * 400
            try:
                with patch.object(c, "run", return_value=types.SimpleNamespace(returncode=0, stdout="", stderr="")), \
                     patch.object(c, "cert_expiry", side_effect=expiry), contextlib.redirect_stdout(io.StringIO()) as out:
                    setup.wait_ready("127.0.0.1", listener.getsockname()[1], timeout=3, tls=tls)
                self.assertIn("gateway-renew", out.getvalue())
            finally:
                with contextlib.suppress(OSError):
                    listener.shutdown(socket.SHUT_RDWR)
                listener.close()

    def initialize(self, cfg, tmp, *, interfaces=None, address=None, replace=False, ufw="ENABLED=yes\n", ready_error=None):
        etc = Path(tmp)
        (etc / "units").mkdir(exist_ok=True)
        (etc / "ufw.conf").write_text(ufw)
        calls = []
        def fake_run(args, **kwargs):
            calls.append(list(args))
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        args = types.SimpleNamespace(family=FAMILY, origin=ORIGIN, address=address, replace=replace)
        interfaces = interfaces or [{"name": "eth0", "ip": "192.168.1.24", "network": "192.168.1.0/24"}]
        with patch.object(c, "P", types.SimpleNamespace(etc=etc, units=etc / "units", app=Path("/opt/omarchy-nas/omarchy-nas.pyz"))), \
             patch.object(c, "load_config", return_value=cfg), patch.object(c, "network_interfaces", return_value=interfaces), \
             patch.object(cli, "root"), patch.object(cli, "copy_app") as copy_app, patch.object(c, "prepare_paths"), \
             patch.object(setup.shutil, "which", return_value="/usr/bin/openssl"), patch.object(setup, "certificates"), \
             patch.object(setup.os, "chown"), patch.object(c, "run", side_effect=fake_run), patch.object(c, "UFW", etc / "ufw.conf"), \
             patch.object(setup, "wait_ready", side_effect=ready_error) as ready, contextlib.redirect_stdout(io.StringIO()) as out:
            try:
                setup.initialize(args)
            finally:
                self.calls, self.copy_app, self.ready, self.out = calls, copy_app, ready, out.getvalue()

    def test_pairing_binds_every_enrolled_share_identity_and_prints_firewall_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.initialize(nas_config(paused=True), tmp)
            saved = json.loads((Path(tmp) / "gateway.json").read_text())
        self.assertEqual(saved["shares"], [
            {"name": "family", "id_type": "uuid", "id": "usb-333", "fs": "apfs", "mode": "ro"},
            {"name": "upload", "id_type": "partuuid", "id": "part-444", "fs": "ext4", "mode": "rw"}])
        self.ready.assert_called_once_with("192.168.1.24")
        self.assertIn(["systemctl", "restart", setup.SERVICE], self.calls)
        self.assertIn("upload（已暂停）", self.out)
        self.assertIn("sudo ufw allow from 192.168.1.0/24 to 192.168.1.24 port 8789 proto tcp", self.out)
        self.assertIn("sudo ufw allow from 192.168.1.0/24 to 192.168.1.24 port 445 proto tcp", self.out)
        self.assertIn("UFW 已启用", self.out)
        self.assertNotIn("ufw", " ".join(" ".join(call) for call in self.calls))
        with tempfile.TemporaryDirectory() as tmp:
            self.initialize(nas_config(), tmp, ufw="ENABLED=no\n")
        self.assertNotIn("UFW 已启用", self.out)

    def test_pairing_requires_an_enabled_share(self):
        cfg = nas_config(); cfg["shares"][0]["enabled"] = False
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(c.NasError, "至少一个"):
            self.initialize(cfg, tmp)
        self.copy_app.assert_not_called()

    def test_failed_readiness_prints_no_success_banner(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(c.NasError, "未就绪"):
            self.initialize(nas_config(), tmp, ready_error=c.NasError("网关未就绪"))
        self.assertNotIn("HTTPS 网关已启动", self.out)
        self.assertEqual(self.out, "")

    def test_paired_host_is_pointed_to_upgrade_and_renew(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "gateway.json").write_text("{}")
            with self.assertRaisesRegex(c.NasError, "upgrade") as raised:
                self.initialize(nas_config(), tmp)
        self.assertIn("gateway-renew", str(raised.exception))
        self.copy_app.assert_not_called()

    def test_changed_interface_or_subnet_names_the_network_command(self):
        cfg = nas_config(); cfg["interface"] = "enp3s0f0"
        moved = [{"name": "wlan0", "ip": "192.168.0.23", "network": "192.168.0.0/24"}]
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(c.NasError) as raised:
            self.initialize(cfg, tmp, interfaces=moved, address="192.168.0.23", replace=True)
        message = str(raised.exception)
        for part in ("enp3s0f0", "192.168.1.0/24", "wlan0 192.168.0.23", "sudo omarchy-nas network"):
            self.assertIn(part, message)
        self.copy_app.assert_not_called()


if __name__ == "__main__":
    unittest.main()
