import copy
import contextlib
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

from nas import core as c
from nas import cli, web


def fixture(fs="apfs"):
    return {"blockdevices": [
        {"name": "/dev/sda", "type": "disk", "size": 10**12, "tran": "sata", "model": "APPLE HDD", "children": [
            {"name": "/dev/sda1", "type": "part", "size": 9*10**11, "fstype": "ext4", "uuid": "system-111", "mountpoints": ["/"], "maj:min": "8:1"},
            {"name": "/dev/sda2", "type": "part", "size": 10**11, "fstype": "ext4", "uuid": "internal-22", "mountpoints": [], "maj:min": "8:2"}]},
        {"name": "/dev/sdc", "type": "disk", "size": 12*10**12, "tran": "usb", "serial": "disk-serial", "model": "DAS", "children": [
            {"name": "/dev/sdc2", "type": "part", "size": 12*10**12-100, "fstype": fs, "uuid": "usb-333", "partuuid": "partition-333", "mountpoints": [], "maj:min": "8:34"}]}
    ]}


def config(fs="apfs", mode="ro"):
    return {"schema": 1, "version": c.VERSION, "user": "charles", "uid": 1000, "gid": 1000,
            "interface": "eth0", "network": "192.168.1.0/24", "shares": [{
                "name": "family", "id_type": "uuid", "id": "usb-333", "fs": fs,
                "size": 12*10**12-100, "serial": "disk-serial", "mode": mode, "enabled": True, "volume": 0}]}


class DetectionTests(unittest.TestCase):
    def test_internal_system_and_spare_sibling_are_excluded(self):
        rows = c.inventory(fixture())
        self.assertFalse(any(r["eligible"] for r in rows if r["disk"] == "/dev/sda"))
        self.assertTrue(next(r for r in rows if r["path"] == "/dev/sdc2")["eligible"])

    def test_usb_boot_drive_is_protected_too(self):
        data = fixture()
        data["blockdevices"][0]["tran"] = "usb"
        rows = c.inventory(data)
        self.assertFalse(any(r["eligible"] for r in rows if r["disk"] == "/dev/sda"))

    def test_encrypted_root_blocks_all_siblings(self):
        data = fixture()
        partition = data["blockdevices"][0]["children"][0]
        partition.update(fstype="crypto_LUKS", mountpoints=[], children=[{"name": "/dev/mapper/root", "type": "crypt", "fstype": "ext4", "size": 100, "mountpoints": ["/"]}])
        self.assertFalse(any(r["eligible"] for r in c.inventory(data) if r["disk"] == "/dev/sda"))

    def test_swap_disk_is_protected(self):
        data = fixture()
        data["blockdevices"][1]["children"].append({"name": "/dev/sdc3", "type": "part", "fstype": "swap", "mountpoints": ["[SWAP]"]})
        self.assertFalse(any(r["eligible"] for r in c.inventory(data) if r["disk"] == "/dev/sdc"))

    def test_apfs_guid_fallback(self):
        data = fixture(None)
        data["blockdevices"][1]["children"][0].update(parttype=c.APFS_GUID, uuid=None)
        dev = next(r for r in c.inventory(data) if r["path"] == "/dev/sdc2")
        self.assertEqual(dev["fs"], "apfs")
        self.assertTrue(dev["eligible"])

    def test_foreign_mount_is_excluded(self):
        data = fixture()
        data["blockdevices"][1]["children"][0]["mountpoints"] = ["/run/media/charles/Disk"]
        self.assertFalse(c.inventory(data)[-1]["eligible"])

    def test_multiple_parents_cannot_be_enrolled(self):
        data = fixture()
        data["blockdevices"].append(copy.deepcopy(data["blockdevices"][1]))
        self.assertFalse(c.inventory(data)[-1]["eligible"])

    def test_control_sequences_removed_from_device_labels(self):
        data = fixture()
        data["blockdevices"][1]["children"][0]["label"] = "Family\n\x1b[31m"
        label = c.inventory(data)[-1]["label"]
        self.assertNotIn("\n", label)
        self.assertNotIn("\x1b", label)


class IdentityTests(unittest.TestCase):
    def test_usb_path_can_change_after_replug(self):
        rows = c.inventory(fixture())
        rows[-1]["path"] = "/dev/sde2"
        self.assertEqual(c.match_device(config()["shares"][0], rows)["path"], "/dev/sde2")

    def test_cloned_uuids_are_ambiguous(self):
        rows = c.inventory(fixture())
        rows.append({**rows[-1], "path": "/dev/sdf2"})
        with self.assertRaisesRegex(c.NasError, "重复"):
            c.match_device(config()["shares"][0], rows)

    def test_changed_serial_or_size_or_fs_rejected(self):
        for key, value in [("serial", "another-disk"), ("size", 123), ("fs", "ext4")]:
            with self.subTest(key=key):
                rows = c.inventory(fixture())
                rows[-1][key] = value
                with self.assertRaises(c.NasError):
                    c.match_device(config()["shares"][0], rows)

    def test_apfs_write_is_rejected_at_config_and_mount(self):
        cfg = config(mode="rw")
        with self.assertRaises(c.NasError): c.validate_config(cfg)
        with self.assertRaises(c.NasError): c.mount_command(cfg["shares"][0], c.inventory(fixture())[-1], cfg)

    def test_names_cannot_inject_samba_or_shell(self):
        for name in ["global", "homes", "../family", "a\n[global]", "a;touch /tmp/pwn", "A B", "a%U", "a"*33]:
            with self.subTest(name=name), self.assertRaises(c.NasError): c.valid_name(name)

    def test_public_networks_cannot_be_exposed(self):
        for network in ["0.0.0.0/0", "8.8.8.0/24", "::/0", "127.0.0.1/32"]:
            cfg = config(); cfg["network"] = network
            with self.subTest(network=network), self.assertRaises(c.NasError): c.validate_config(cfg)

    def test_duplicate_volume_registration_is_rejected(self):
        cfg = config(); cfg["shares"].append({**cfg["shares"][0], "name": "other"})
        with self.assertRaises(c.NasError): c.validate_config(cfg)


class MountTests(unittest.TestCase):
    def test_ext4_readonly_disables_journal_replay(self):
        cfg = config("ext4")
        args = c.mount_command(cfg["shares"][0], c.inventory(fixture("ext4"))[-1], cfg)
        self.assertIn("ro,nosuid,nodev,noexec,noload", args)

    def test_xfs_readonly_disables_recovery(self):
        cfg = config("xfs")
        args = c.mount_command(cfg["shares"][0], c.inventory(fixture("xfs"))[-1], cfg)
        self.assertIn("ro,nosuid,nodev,noexec,norecovery", args)

    def test_ntfs_does_not_remove_hibernation_file(self):
        cfg = config("ntfs", "rw")
        args = c.mount_command(cfg["shares"][0], c.inventory(fixture("ntfs"))[-1], cfg)
        self.assertIn("norecover", " ".join(args))
        self.assertNotIn("remove_hiberfile", " ".join(args))

    def test_unmounted_path_is_never_a_valid_share(self):
        with patch.object(c, "mount_info", return_value=None), self.assertRaisesRegex(c.NasError, "没有真实挂载"):
            c.verify_mount(config()["shares"][0], c.inventory(fixture())[-1])

    def test_wrong_mount_source_and_mode_are_rejected(self):
        cfg = config("ext4"); dev = c.inventory(fixture("ext4"))[-1]
        cases = [{"fstype": "ext4", "maj:min": "8:99", "options": "ro,nosuid,nodev,noexec"},
                 {"fstype": "ext4", "maj:min": dev["major_minor"], "options": "rw,nosuid,nodev,noexec"},
                 {"fstype": "ext4", "maj:min": dev["major_minor"], "options": "ro"}]
        for info in cases:
            with self.subTest(info=info), patch.object(c, "mount_info", return_value=info), self.assertRaises(c.NasError):
                c.verify_mount(cfg["shares"][0], dev)

    def test_apfs_source_and_readonly_flags_are_verified(self):
        cfg = config(); dev = c.inventory(fixture())[-1]
        info = {"fstype": "fuse.omarchy-apfs", "source": dev["path"], "options": "ro,nosuid,nodev,noexec,relatime"}
        with patch.object(c, "mount_info", return_value=info):
            self.assertEqual(c.verify_mount(cfg["shares"][0], dev), info)
        info["source"] = "/dev/sda1"
        with patch.object(c, "mount_info", return_value=info), self.assertRaises(c.NasError):
            c.verify_mount(cfg["shares"][0], dev)

    def test_fuse_daemon_is_owned_by_its_own_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = c.Paths(mounts=Path(tmp))
            target = Path(tmp) / "family"; target.mkdir()
            cfg = config()
            with patch.object(c, "mount_info", return_value=None), patch.object(c, "secure_dir"), patch.object(c, "verify_mount"), \
                 patch.object(c, "tool", return_value="/usr/bin/apfs-fuse"), patch.object(c, "run", return_value=types.SimpleNamespace(returncode=0)) as run:
                c.ensure_mounted(cfg["shares"][0], cfg, c.inventory(fixture(), p=p), p)
            args = run.call_args_list[0].args[0]
            self.assertEqual(args[0], "systemd-run")
            self.assertIn("--property=Type=forking", args)

    def test_permission_failure_unmounts_new_volume(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = c.Paths(mounts=Path(tmp)); (Path(tmp)/"family").mkdir()
            cfg=config("ext4")
            calls=[]
            def fail_permission(args, **kw):
                calls.append(args)
                if args[0]=="runuser": raise c.NasError("permission denied")
            with patch.object(c,"mount_info",return_value=None), patch.object(c,"secure_dir"), patch.object(c,"verify_mount"), patch.object(c,"run",side_effect=fail_permission):
                with self.assertRaises(c.NasError): c.ensure_mounted(cfg["shares"][0],cfg,c.inventory(fixture("ext4")),p)
            self.assertEqual(calls[-1][0],"umount")


class ServiceTests(unittest.TestCase):
    def test_config_is_authenticated_and_has_mount_guard(self):
        out = c.smb_config(config())
        self.assertIn("guest ok = no", out)
        self.assertIn("root preexec close = yes", out)
        self.assertIn("follow symlinks = no", out)
        self.assertIn("smb encrypt = required", out)
        self.assertIn("hosts deny = ALL", out)
        self.assertIn("/var/lib/omarchy-nas/private/passdb.tdb", out)

    def test_paused_share_is_not_exported(self):
        cfg = config(); cfg["shares"][0]["enabled"] = False
        self.assertNotIn("[family]", c.smb_config(cfg))
        with patch.object(c, "load_config", return_value=cfg), self.assertRaises(c.NasError): c.guard("family")

    def test_no_service_conflict_can_stop_existing_samba(self):
        units = c.unit_files(config())
        self.assertFalse(any("Conflicts=" in unit for unit in units.values()))
        self.assertIn("User=charles", units["omarchy-nas-panel.service"])

    def test_existing_samba_blocks_install(self):
        with patch.object(cli, "service_active", return_value=True), self.assertRaisesRegex(c.NasError, "已有"):
            cli.check_conflict()

    def test_smb_config_failure_keeps_last_valid_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = c.Paths(etc=Path(tmp)); p.smb.write_text("last good")
            with patch.object(c, "run", side_effect=c.NasError("parse failed")), self.assertRaises(c.NasError):
                c.write_smb(config(), p)
            self.assertEqual(p.smb.read_text(), "last good")
            self.assertFalse((p.etc / "smb.conf.next").exists())

    def test_password_is_stdin_only(self):
        with patch.object(cli.getpass, "getpass", side_effect=["example-secret", "example-secret"]), patch.object(c,"run") as run:
            cli.set_password(config())
        self.assertNotIn("example-secret", " ".join(run.call_args.args[0]))
        self.assertEqual(run.call_args.kwargs["input"], "example-secret\nexample-secret\n")

    def test_reconcile_records_failure_instead_of_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = c.Paths(run=Path(tmp))
            with patch.object(c,"locked",return_value=contextlib.nullcontext()), patch.object(c,"load_config",return_value=config()), \
                 patch.object(c,"inventory",return_value=[]), patch.object(c,"ensure_mounted",side_effect=c.NasError("磁盘离线")):
                result=c.reconcile(p)
            self.assertFalse(result[0]["online"])
            self.assertEqual(json.loads((Path(tmp)/"status.json").read_text())["shares"][0]["error"],"磁盘离线")

    def test_symlinked_state_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            target=Path(tmp)/"real"; target.mkdir(); link=Path(tmp)/"link"; link.symlink_to(target)
            with patch.object(c.os,"geteuid",return_value=1000), self.assertRaises(c.NasError): c.secure_dir(link)


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=web.Server(("127.0.0.1",0),web.make_handler(demo=True))
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True); cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join()

    def request(self,path,method="GET",headers=None):
        conn=http.client.HTTPConnection("127.0.0.1",self.server.server_port)
        conn.request(method,path,headers=headers or {})
        r=conn.getresponse(); data=r.read(); result=(r.status,dict(r.getheaders()),data); conn.close(); return result

    def test_demo_api_is_explicitly_marked_and_assets_load(self):
        status,headers,data=self.request("/api/status")
        self.assertEqual(status,200); self.assertTrue(json.loads(data)["demo"])
        for path in ("/","/app.js","/style.css"):
            status,headers,data=self.request(path)
            self.assertEqual(status,200); self.assertTrue(len(data)>100)
            self.assertIn("frame-ancestors 'none'",headers["Content-Security-Policy"])

    def test_untrusted_host_is_denied(self):
        self.assertEqual(self.request("/api/status",headers={"Host":"evil.example"})[0],403)

    def test_cross_site_request_is_denied(self):
        self.assertEqual(self.request("/api/status",headers={"Sec-Fetch-Site":"cross-site"})[0],403)

    def test_no_write_or_arbitrary_file_endpoints(self):
        self.assertEqual(self.request("/api/status",method="POST")[0],501)
        self.assertEqual(self.request("/../../etc/passwd")[0],404)


if __name__ == "__main__":
    unittest.main()
