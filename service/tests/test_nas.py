import copy
import contextlib
import errno
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
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


def result(returncode=0, stdout=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


BY_UUID = "/dev/disk/by-uuid/usb-333"
real_realpath = os.path.realpath


def udev(node="/dev/sdc2"):
    # The container has no /dev/disk/by-uuid; model udev pointing the link at `node`.
    return patch.object(c.os.path, "realpath", side_effect=lambda path, *a, **k: node if path == BY_UUID else real_realpath(path, *a, **k))


def node_exists(node, present):
    # Model udev keeping or removing a device node, independent of the test host's /dev.
    real = os.path.exists
    return patch.object(c.os.path, "exists", side_effect=lambda path: present if path == node else real(path))


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
            with patch.object(c, "mount_info", return_value=None), patch.object(c, "secure_dir"), patch.object(c, "verify_mount"), udev(), \
                 patch.object(c, "tool", return_value="/usr/bin/apfs-fuse"), patch.object(c, "run", return_value=types.SimpleNamespace(returncode=0)) as run:
                c.ensure_mounted(cfg["shares"][0], cfg, c.inventory(fixture(), p=p), p)
            args = run.call_args_list[0].args[0]
            self.assertEqual(args[0], "systemd-run")
            self.assertIn("--property=Type=forking", args)
            self.assertEqual(args[-2:], ["/dev/sdc2", str(target)])

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

    def mount_new(self, fs, mode, access, *, claim=False, contents=("lost+found",)):
        # Mount, verify and access are simulated; the volume root is a real temp directory.
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        p = c.Paths(mounts=Path(tmp.name)); target = Path(tmp.name) / "family"; target.mkdir()
        cfg, codes, self.calls = config(fs, mode), iter(access), []
        def fake_run(args, **kwargs):
            self.calls.append(args)
            if args[0] in {"mount", "systemd-run"}:
                for name in contents:
                    (target / name).mkdir()
            return result(next(codes) if args[0] == "runuser" else 0)
        with patch.object(c, "mount_info", return_value=None), patch.object(c, "secure_dir"), patch.object(c, "verify_mount"), udev(), \
             patch.object(c, "tool", side_effect=lambda name: "/usr/bin/" + name), \
             patch.object(c, "run", side_effect=fake_run), patch.object(c.os, "fchown") as chown, patch.object(c.os, "fchmod") as chmod:
            try:
                return c.ensure_mounted(cfg["shares"][0], cfg, c.inventory(fixture(fs)), p, claim=claim)
            finally:
                self.chown, self.chmod = chown, chmod

    def test_rw_ext4_root_not_writable_gives_actionable_error(self):
        with self.assertRaises(c.NasError) as raised:
            self.mount_new("ext4", "rw", [1])
        message = str(raised.exception)
        for part in ("--claim-root", "sudo chown 1000:1000 /mnt", "chmod 0700", "mkfs.ext4 -E root_owner=1000:1000", "不递归"):
            self.assertIn(part, message)
        self.assertEqual(self.calls[-1][0], "umount")
        self.chown.assert_not_called()

    def test_claim_root_changes_only_an_empty_fresh_root(self):
        for fs in ("ext4", "xfs"):
            with self.subTest(fs=fs):
                self.mount_new(fs, "rw", [1, 0], claim=True)
                self.chown.assert_called_once()
                self.assertEqual(self.chown.call_args.args[1:], (1000, 1000))
                self.chmod.assert_called_once()
                self.assertEqual(self.chmod.call_args.args[1:], (0o700,))
                self.assertNotIn("umount", [call[0] for call in self.calls])

    def test_claim_root_refuses_a_root_with_data(self):
        with self.assertRaisesRegex(c.NasError, "未做任何修改"):
            self.mount_new("ext4", "rw", [1], claim=True, contents=("lost+found", "photos"))
        self.chown.assert_not_called(); self.chmod.assert_not_called()
        self.assertEqual(self.calls[-1][0], "umount")
        with self.assertRaisesRegex(c.NasError, "仍无法访问"):
            self.mount_new("xfs", "rw", [1, 1], claim=True, contents=())
        self.chown.assert_called_once()

    def test_claim_root_never_applies_to_apfs_or_readonly(self):
        with self.assertRaises(c.NasError) as raised:
            self.mount_new("ext4", "ro", [1], claim=True)
        self.chown.assert_not_called()
        self.assertNotIn("--claim-root", str(raised.exception))
        device = c.inventory(fixture())[-1]
        for fs, writable in (("apfs", False), ("ext4", False), ("exfat", True)):
            with self.subTest(fs=fs), self.assertRaisesRegex(c.NasError, "--claim-root"):
                cli.enroll({**device, "fs": fs}, types.SimpleNamespace(name="family", writable=writable, apfs_volume=0, claim_root=True), config())

    def test_stale_apfs_mount_after_replug_is_rejected(self):
        data = fixture(); data["blockdevices"][1]["children"][0].update(name="/dev/sdd2", **{"maj:min": "8:50"})
        dev = c.inventory(data)[-1]
        info = {"fstype": "fuse.omarchy-apfs", "source": "/dev/sdc2", "options": "ro,nosuid,nodev,noexec"}
        with udev("/dev/sdd2"), node_exists("/dev/sdc2", False), patch.object(c, "mount_info", return_value=info), self.assertRaises(c.NasError) as raised:
            c.verify_mount(config()["shares"][0], dev)
        for part in ("陈旧挂载", "/dev/sdc2", "/dev/sdd2", "sudo omarchy-nas pause family 再 resume family"):
            self.assertIn(part, str(raised.exception))
        # Another live device mounted there is still a plain refusal, not a replug.
        with udev("/dev/sdd2"), node_exists("/dev/sdc2", True), patch.object(c, "mount_info", return_value=info), self.assertRaisesRegex(c.NasError, "^FUSE 挂载源未能核实"):
            c.verify_mount(config()["shares"][0], dev)

    def test_ext4_replug_reports_stale_mount_with_remedy(self):
        cfg = config("ext4", "rw")
        data = fixture("ext4"); data["blockdevices"][1]["children"][0].update(name="/dev/sdd2", **{"maj:min": "8:50"})
        devices = c.inventory(data)
        info = {"fstype": "ext4", "source": "/dev/sdc2", "maj:min": "8:34", "options": "rw,nosuid,nodev,noexec"}
        calls = []
        with patch.object(c, "mount_info", return_value=info), patch.object(c, "load_config", return_value=cfg), \
             patch.object(c, "inventory", return_value=devices), patch.object(c, "probe") as probe, \
             patch.object(c, "run", side_effect=lambda args, **kw: calls.append(args)):
            for exists, expected in ((False, "陈旧挂载"), (True, "挂载设备与登记设备不一致")):
                for check in (lambda: c.ensure_mounted(cfg["shares"][0], cfg, devices), lambda: c.guard("family")):
                    with self.subTest(exists=exists), node_exists("/dev/sdc2", exists), self.assertRaisesRegex(c.NasError, "^" + expected) as raised:
                        check()
                    self.assertEqual("pause family" in str(raised.exception), not exists)
        probe.assert_not_called()
        self.assertEqual(calls, [])

    def test_bind_mount_source_is_a_mismatch_not_a_stale_mount(self):
        dev = {"path": "/dev/sdd2", "major_minor": "8:50"}
        for fs, fstype, expected in (("ext4", "ext4", "挂载设备与登记设备不一致"), ("ntfs", "fuse.ntfs-3g", "FUSE 挂载源未能核实；拒绝共享")):
            share = config(fs, "rw")["shares"][0]
            info = {"fstype": fstype, "source": "/dev/sda2[/home/x]", "maj:min": "8:2", "options": "rw,nosuid,nodev,noexec"}
            with self.subTest(fs=fs), patch.object(c, "mount_info", return_value=info), node_exists("/dev/sda2[/home/x]", False), \
                 self.assertRaises(c.NasError) as raised:
                c.verify_mount(share, dev)
            self.assertEqual(str(raised.exception), expected)

    def test_apfs_mount_command_uses_resolved_node(self):
        cfg = config(); dev = c.inventory(fixture())[-1]
        with udev("/dev/sdc2"):
            self.assertEqual(c.mount_command(cfg["shares"][0], dev, cfg)[-2], "/dev/sdc2")
        with udev("/dev/sdd2"), self.assertRaisesRegex(c.NasError, "拒绝挂载"):
            c.mount_command(cfg["shares"][0], dev, cfg)

    def test_dead_fuse_mount_reports_stale_not_permission(self):
        with patch.object(c.os, "statvfs", side_effect=OSError(errno.ENOTCONN, "Transport endpoint is not connected")):
            with self.assertRaises(c.NasError) as raised:
                self.mount_new("apfs", "ro", [0])
        self.assertIn("陈旧挂载", str(raised.exception))
        self.assertIn("pause family", str(raised.exception))
        self.assertNotIn("权限", str(raised.exception))
        self.assertNotIn("runuser", [call[0] for call in self.calls])

    def test_stacked_mounts_are_reported_not_chmodded(self):
        stacked = json.dumps({"filesystems": [{"target": "/srv/omarchy-nas/family", "source": "/dev/sdc2"},
                                              {"target": "/srv/omarchy-nas/family", "source": "/dev/sdd1"}]})
        with tempfile.TemporaryDirectory() as tmp:
            p = c.Paths(mounts=Path(tmp)); (Path(tmp) / "family").mkdir()
            with patch.object(c, "run", return_value=result(0, stacked)), patch.object(c, "secure_dir") as secure:
                with self.assertRaisesRegex(c.NasError, "多层挂载"):
                    c.ensure_mounted(config("ext4", "rw")["shares"][0], config("ext4", "rw"), c.inventory(fixture("ext4")), p)
            secure.assert_not_called()
        for out in (result(1), result(0, '{"filesystems": []}')):
            with patch.object(c, "run", return_value=out):
                self.assertIsNone(c.mount_info("/srv/omarchy-nas/family"))

    def test_unreported_mount_root_is_not_chmodded(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = c.Paths(mounts=Path(tmp)); target = Path(tmp) / "family"; target.mkdir()
            real_lstat = os.lstat
            def lstat(path, *args, **kwargs):
                info = real_lstat(path, *args, **kwargs)
                return os.stat_result([*info[:2], info.st_dev + 1, *info[3:]]) if Path(path) == target else info
            with patch.object(c, "mount_info", return_value=None), patch.object(c.os, "lstat", side_effect=lstat), \
                 patch.object(c, "secure_dir") as secure, self.assertRaisesRegex(c.NasError, "挂载根目录"):
                c.ensure_mounted(config("ext4", "rw")["shares"][0], config("ext4", "rw"), c.inventory(fixture("ext4")), p)
            secure.assert_not_called()


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

    def test_smb_binds_registered_interface_and_filters_sources(self):
        # A CIDR token binds only the lowest local address in it, which may belong to another NIC.
        out = c.smb_config(config())
        self.assertIn("interfaces = lo eth0\n", out)
        self.assertNotIn("interfaces = lo 192.168.1.0/24", out)
        self.assertIn("bind interfaces only = yes\n", out)
        self.assertIn("hosts allow = 127.0.0.1 192.168.1.0/24\n", out)
        self.assertIn("hosts deny = ALL\n", out)

    def test_panel_unit_is_sandboxed_without_hiding_devices(self):
        panel = c.unit_files(config())["omarchy-nas-panel.service"]
        for line in ("ProtectHome=yes", "ProtectSystem=full", "PrivateTmp=yes", "ProtectKernelTunables=yes", "ProtectKernelModules=yes",
                     "ProtectControlGroups=yes", "RestrictNamespaces=yes", "LockPersonality=yes", "RestrictRealtime=yes",
                     "SystemCallArchitectures=native", "InaccessiblePaths=-/run/docker.sock", "LimitNOFILE=1024", "TasksMax=256", "NoNewPrivileges=true"):
            self.assertIn(line + "\n", panel)
        for line in ("PrivateDevices", "ProtectSystem=strict", "SystemCallFilter"):
            self.assertNotIn(line, panel)
        self.assertIn("\n\n[Install]", panel)

    def test_network_command_moves_subnet_and_keeps_shares(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = c.Paths(etc=Path(tmp), data=Path(tmp) / "data", run=Path(tmp) / "run")
            cfg = config(); cfg["shares"].append({**config("ext4", "rw")["shares"][0], "name": "upload", "id": "usb-444", "enabled": False})
            p.config.write_text(json.dumps(cfg))
            calls = []
            def fake_run(args, **kwargs):
                calls.append([str(a) for a in args]); return result()
            load, save, write = c.load_config, c.save_config, c.write_smb
            wlan = {"name": "wlan0", "ip": "192.168.0.23", "network": "192.168.0.0/24"}
            with patch.object(c, "P", p), patch.object(cli, "root"), patch.object(c, "locked", return_value=contextlib.nullcontext()), \
                 patch.object(c, "load_config", side_effect=lambda: load(p)), patch.object(c, "save_config", side_effect=lambda x: save(x, p)), \
                 patch.object(c, "write_smb", side_effect=lambda x: write(x, p)), patch.object(c, "run", side_effect=fake_run), patch.object(c, "UFW", p.etc / "ufw.conf"), \
                 patch.object(c, "network_interfaces", return_value=[{"name": "eth0", "ip": "10.0.0.5", "network": "10.0.0.0/24"}, wlan]), \
                 patch("builtins.input", return_value="1"), contextlib.redirect_stdout(io.StringIO()) as out:
                cli.renetwork(types.SimpleNamespace(interface="wlan0"))
            saved = json.loads(p.config.read_text())
            self.assertEqual((saved["interface"], saved["network"]), ("wlan0", "192.168.0.0/24"))
            self.assertEqual(saved["shares"], cfg["shares"])
            smb = p.smb.read_text()
            self.assertIn("interfaces = lo wlan0\n", smb)
            self.assertIn("hosts allow = 127.0.0.1 192.168.0.0/24", smb)
            self.assertEqual(calls[0][0], "testparm")
            self.assertIn(["systemctl", "try-restart", c.SERVICE], calls)
            self.assertIn("sudo ufw allow from 192.168.0.0/24 to 192.168.0.23 port 445 proto tcp", out.getvalue())
            self.assertNotIn("8789", out.getvalue())

    def test_upgrade_reinstalls_the_program_and_units_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p = c.Paths(etc=root / "etc", units=root / "units", app=root / "opt/omarchy-nas.pyz")
            p.etc.mkdir(parents=True); p.units.mkdir()
            p.config.write_text(json.dumps(config()))
            before = p.config.read_text()
            calls = []
            def fake_run(args, **kwargs):
                calls.append([str(a) for a in args]); return result()
            with patch.object(c, "P", p), patch.object(cli, "root"), patch.object(cli, "copy_app") as copy_app, \
                 patch.object(c, "load_config", return_value=config()), patch.object(c, "prepare_paths"), patch.object(c, "write_smb") as smb, \
                 patch.object(c, "run", side_effect=fake_run), contextlib.redirect_stdout(io.StringIO()):
                cli.upgrade()
            copy_app.assert_called_once(); smb.assert_called_once()
            self.assertEqual(p.config.read_text(), before)
            self.assertIn(["systemctl", "daemon-reload"], calls)
            self.assertIn(["systemctl", "try-restart", c.SERVICE, "omarchy-nas-panel.service"], calls)
            self.assertIn("ProtectHome=yes", (p.units / "omarchy-nas-panel.service").read_text())
            self.assertEqual(sorted(u.name for u in p.units.iterdir() if "gateway" in u.name), [])

    def test_doctor_lists_the_smb_port_and_ufw_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            ufw = Path(tmp) / "ufw.conf"
            for text, state in (("ENABLED=yes\n", "UFW：已启用"), ("ENABLED=no\n", "UFW：未启用"), (None, "UFW：未检测到")):
                if text: ufw.write_text(text)
                else: ufw.unlink()
                with self.subTest(state=state), patch.object(c, "UFW", ufw), patch.object(c, "inventory", return_value=[]), \
                     patch.object(c, "network_interfaces", return_value=[{"name": "eth0", "ip": "192.168.1.24", "network": "192.168.1.0/24"}]), \
                     patch.object(c, "load_config", return_value=config()), contextlib.redirect_stdout(io.StringIO()) as out:
                    cli.doctor()
                self.assertIn(state, out.getvalue())
                self.assertIn("TCP 445", out.getvalue()); self.assertNotIn("8789", out.getvalue())
                self.assertIn("sudo ufw allow from 192.168.1.0/24 to 192.168.1.24 port 445 proto tcp", out.getvalue())

    def test_version_strings_agree(self):
        base = Path(__file__).resolve().parents[1]
        self.assertIn(c.VERSION, (base / "README.md").read_text().splitlines()[0])
        self.assertIn(f"v{c.VERSION}", (base / "nas/assets/index.html").read_text())

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
        self.assertNotIn("gateway_certificate",json.loads(data))
        for path in ("/","/app.js","/style.css"):
            status,headers,data=self.request(path)
            self.assertEqual(status,200); self.assertTrue(len(data)>100)
            self.assertIn("frame-ancestors 'none'",headers["Content-Security-Policy"])
        self.assertIn(f'<span id="version">v{c.VERSION}</span>'.encode(),self.request("/")[2])
        for path in ("/Geist-Variable.woff2","/GeistMono-Variable.woff2"):
            status,headers,data=self.request(path)
            self.assertEqual((status,headers["Content-Type"],data[:4]),(200,"font/woff2",b"wOF2"))
        # The public status page says nothing about Family Dashboard or its gateway.
        page=self.request("/")[2].decode()+self.request("/app.js")[2].decode()
        for word in ("Family Dashboard","gateway-renew","gateway-init"):
            self.assertNotIn(word,page)

    def test_untrusted_host_is_denied(self):
        self.assertEqual(self.request("/api/status",headers={"Host":"evil.example"})[0],403)

    def test_cross_site_request_is_denied(self):
        self.assertEqual(self.request("/api/status",headers={"Sec-Fetch-Site":"cross-site"})[0],403)

    def test_no_write_or_arbitrary_file_endpoints(self):
        self.assertEqual(self.request("/api/status",method="POST")[0],501)
        self.assertEqual(self.request("/../../etc/passwd")[0],404)


if __name__ == "__main__":
    unittest.main()
