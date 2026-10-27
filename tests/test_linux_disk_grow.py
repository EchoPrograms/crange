import copy
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from crange_linux_disk_grow import (
    GIB, GPT_MIN_TAIL, LinuxDiskGrowError, build_linux_grow_script,
    plan_linux_disk_grow,
)


def measured(**overrides):
    disk = 32 * GIB
    offset = 1024 * 1024
    partition_bytes = disk - offset - GPT_MIN_TAIL
    result = {
        "disk_bytes": disk, "filesystem": "btrfs",
        "filesystem_used_bytes": 30 * GIB, "btrfs_data_used_bytes": 28 * GIB,
        "reserved_overhead_bytes": GIB, "filesystem_offset_bytes": offset,
        "filesystem_end_bytes": offset + partition_bytes, "partition_table": "gpt",
        "tail_reserve_bytes": GPT_MIN_TAIL, "single_device_root_btrfs": True,
        "btrfs_device_count": 1, "encrypted": False, "lvm": False,
        "root_partition_is_last": True, "disk_device": "/dev/sda",
        "root_partition_device": "/dev/sda2",
        "disk_guid": "11111111-2222-3333-4444-555555555555",
        "partition_number": 2,
        "partition_guid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "filesystem_uuid": "01234567-89ab-cdef-0123-456789abcdef",
        "filesystem_partition_bytes": partition_bytes,
        "sector_bytes": 512,
    }
    result.update(overrides)
    return result


class LinuxDiskGrowTests(unittest.TestCase):
    def test_effective_usage_overhead_and_16_gib_free_determine_whole_gib_target(self):
        plan = plan_linux_disk_grow(measured())
        self.assertEqual(plan["decision"], "grow")
        self.assertGreaterEqual(plan["estimated_usable_free_bytes"], 16 * GIB)
        self.assertEqual(plan["target_disk_bytes"] % GIB, 0)
        self.assertGreaterEqual(plan["target_disk_gib"], 48)
        self.assertLessEqual(plan["target_disk_gib"], 64)

    def test_data_used_alone_is_not_accepted_as_effective_usage(self):
        with self.assertRaises(LinuxDiskGrowError):
            plan_linux_disk_grow(measured(filesystem_used_bytes=None))

    def test_rejects_encryption_lvm_multiple_devices_and_non_root_proof(self):
        for key, value in (("encrypted", True), ("lvm", True),
                           ("btrfs_device_count", 2),
                           ("single_device_root_btrfs", False),
                           ("root_partition_is_last", False)):
            with self.subTest(key=key):
                with self.assertRaises(LinuxDiskGrowError):
                    plan_linux_disk_grow(measured(**{key: value}))

    def test_rejects_wrong_partition_identity_or_bounds(self):
        for change in (
            {"root_partition_device": "/dev/sda3"}, {"partition_number": 3},
            {"filesystem_end_bytes": 32 * GIB},
            {"disk_guid": "bad; touch /tmp/pwned"},
            {"disk_device": "/dev/mapper/root"}, {"tail_reserve_bytes": 0},
        ):
            with self.subTest(change=change):
                with self.assertRaises(LinuxDiskGrowError):
                    plan_linux_disk_grow(measured(**change))

    def test_initial_source_must_be_exactly_32_gib(self):
        with self.assertRaises(LinuxDiskGrowError):
            plan_linux_disk_grow(measured(disk_bytes=31 * GIB))

    def test_script_is_fixed_and_bound_to_one_identity(self):
        m = measured()
        script = build_linux_grow_script(m, plan_linux_disk_grow(m))
        for required in ("findmnt -n -o SOURCE /", "root partition is no longer final",
                         "root partition start changed", "host disk growth to the exact target is required",
                         "-ge 17179869184", "parted -s \"$DISK\" unit B resizepart",
                         "btrfs filesystem resize max /", m["disk_guid"], m["partition_guid"]):
            self.assertIn(required, script)
        self.assertNotIn("eval ", script)

    def test_script_builder_rejects_forged_plan(self):
        m = measured()
        forged = copy.deepcopy(plan_linux_disk_grow(m))
        forged["target_disk_bytes"] += GIB
        with self.assertRaises(LinuxDiskGrowError):
            build_linux_grow_script(m, forged)

    def _run_fake(self, *, disk_bytes, part_bytes, bad_guid=False, available=17 * GIB):
        m = measured()
        plan = plan_linux_disk_grow(m)
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        state, log = root / "state", root / "writes"
        state.write_text(f"{disk_bytes} {part_bytes}\n")
        bindir = root / "bin"
        bindir.mkdir()
        common = '''#!/bin/bash
+set -eu
+read_state() { read -r disk part < "$STATE"; }
+write_log() { printf '%s\\n' "$*" >> "$WRITES"; }
+'''.replace("\n+", "\n")
        sgdisk_output = ("99999999-2222-3333-4444-555555555555" if bad_guid
                         else "11111111-2222-3333-4444-555555555555")
        scripts = {
            "blockdev": common + '''case "$1" in
+--getsize64) read_state; if [[ "$2" == /dev/sda ]]; then echo "$disk"; else echo "$part"; fi;;
+--getss) echo 512;; *) exit 2;; esac
+'''.replace("\n+", "\n"),
            "findmnt": common + '''if [[ "$*" == *FSTYPE* ]]; then echo btrfs; else echo /dev/sda2; fi
+'''.replace("\n+", "\n"),
            "blkid": common + '''echo 01234567-89ab-cdef-0123-456789abcdef
+'''.replace("\n+", "\n"),
            "sgdisk": common + f'''if [[ "$1" == -p ]]; then
+echo 'Disk identifier (GUID): {sgdisk_output}'
+echo '2 2048 5000 5000 8300 root'
+else echo 'Partition unique GUID: aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'; fi
+[[ "$1" != -e ]] || write_log "sgdisk $*"
+'''.replace("\n+", "\n"),
            "lsblk": common + '''if [[ "$*" == *TYPE* ]]; then
+if [[ "$*" == *sda2* ]]; then echo part; else echo disk; fi
+else echo 2048; fi
+'''.replace("\n+", "\n"),
            "btrfs": common + '''if [[ "$*" == *show* ]]; then echo ' devid 1 size 1 used 1 path /dev/sda2'; else echo 'btrfs usage'; fi
+'''.replace("\n+", "\n"),
            "parted": common + '''read_state; write_log "parted $*"; printf '%s %s\\n' "$disk" "$NEWPART" > "$STATE"
+'''.replace("\n+", "\n"),
            "udevadm": common + '''write_log "udevadm $*"
+'''.replace("\n+", "\n"),
            "df": common + '''echo "$AVAILABLE"
+'''.replace("\n+", "\n"),
        }
        for name, content in scripts.items():
            path = bindir / name
            path.write_text(content)
            path.chmod(0o755)
        guest = root / "grow.sh"
        guest.write_text(build_linux_grow_script(m, plan))
        guest.chmod(0o755)
        env = os.environ.copy()
        env.update({"PATH": f"{bindir}:/usr/bin:/bin", "STATE": str(state),
                    "WRITES": str(log), "NEWPART": str(plan["target_partition_bytes"]),
                    "AVAILABLE": str(available)})
        proc = subprocess.run([str(guest)], env=env, text=True, capture_output=True)
        return temp, proc, log.read_text() if log.exists() else "", state.read_text(), plan

    def test_original_host_disk_exits_before_any_partition_write(self):
        m = measured()
        temp, proc, writes, _, _ = self._run_fake(
            disk_bytes=m["disk_bytes"], part_bytes=m["filesystem_partition_bytes"])
        try:
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("host disk growth", proc.stderr)
            self.assertEqual(writes, "")
        finally:
            temp.cleanup()

    def test_exact_target_host_disk_grows_known_partition(self):
        m = measured()
        plan = plan_linux_disk_grow(m)
        temp, proc, writes, state, _ = self._run_fake(
            disk_bytes=plan["target_disk_bytes"], part_bytes=m["filesystem_partition_bytes"])
        try:
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(state.split()[1], str(plan["target_partition_bytes"]))
            self.assertIn("sgdisk -e /dev/sda", writes)
            self.assertIn("parted -s /dev/sda unit B resizepart 2", writes)
        finally:
            temp.cleanup()

    def test_already_grown_partition_is_idempotent(self):
        m = measured()
        plan = plan_linux_disk_grow(m)
        temp, proc, writes, _, _ = self._run_fake(
            disk_bytes=plan["target_disk_bytes"], part_bytes=plan["target_partition_bytes"])
        try:
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(writes, "")
        finally:
            temp.cleanup()

    def test_identity_mismatch_fails_before_any_write(self):
        m = measured()
        plan = plan_linux_disk_grow(m)
        temp, proc, writes, _, _ = self._run_fake(
            disk_bytes=plan["target_disk_bytes"],
            part_bytes=m["filesystem_partition_bytes"], bad_guid=True)
        try:
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(writes, "")
        finally:
            temp.cleanup()

    def test_post_grow_filesystem_free_must_be_measured_at_or_above_16_gib(self):
        m = measured()
        plan = plan_linux_disk_grow(m)
        temp, proc, _, _, _ = self._run_fake(
            disk_bytes=plan["target_disk_bytes"],
            part_bytes=plan["target_partition_bytes"], available=15 * GIB)
        try:
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("does not prove 16 GiB available", proc.stderr)
        finally:
            temp.cleanup()


if __name__ == "__main__":
    unittest.main()
