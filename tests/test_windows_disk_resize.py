import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from crange_windows_disk_resize import (  # noqa: E402
    GIB,
    MIB,
    RECOVERY_BYTES,
    WindowsDiskResizeError,
    build_prepare_script,
    build_verify_script,
    plan_guest_layout,
)


JOB = "b89023ef-2ced-412c-9dc9-c29e6d260f9e"


class WindowsDiskResizeTests(unittest.TestCase):
    def test_server_layout_retains_winre_and_role_headroom(self):
        # Representative 19 GiB measured usage and the supplied 21.7 GiB
        # minimum. 32 GiB leaves roughly 12 GiB for C:, WinRE and GPT tail.
        plan = plan_guest_layout(32, "server", used_bytes=19 * GIB,
                                 supported_min_bytes=22 * GIB)
        self.assertGreaterEqual(plan["free_bytes"], 8 * GIB)
        self.assertLessEqual(plan["free_bytes"], 12 * GIB)
        self.assertEqual(plan["recovery_size_bytes"], RECOVERY_BYTES)
        self.assertEqual(plan["recovery_offset_bytes"] + RECOVERY_BYTES,
                         plan["target_disk_bytes"] - MIB)
        self.assertGreaterEqual(plan["recovery_offset_bytes"],
                                plan["c_offset_bytes"] + plan["c_size_bytes"])

    def test_jumpbox_layout_provides_at_least_sixteen_gib_free(self):
        plan = plan_guest_layout(38, "jumpbox", used_bytes=20 * GIB,
                                 supported_min_bytes=22 * GIB)
        self.assertGreaterEqual(plan["free_bytes"], 16 * GIB)
        self.assertEqual(plan["recovery_offset_bytes"] % MIB, 0)

    def test_private_base_layout_allows_22_gib_with_preserved_winre_and_headroom(self):
        plan = plan_guest_layout(22, "base", used_bytes=11_416_739_840,
                                 supported_min_bytes=21_812_527_104)
        self.assertGreaterEqual(plan["free_bytes"], 8 * GIB)
        self.assertEqual(plan["target_disk_bytes"], 22 * GIB)
        self.assertEqual(plan["recovery_size_bytes"], RECOVERY_BYTES)
        self.assertEqual(plan["recovery_offset_bytes"] + RECOVERY_BYTES,
                         22 * GIB - MIB)
        self.assertGreaterEqual(plan["c_size_bytes"], 21_812_527_104)

    def test_private_base_role_rejects_out_of_bounds_minimum_and_headroom(self):
        for target, used, minimum in (
                (19, 11 * GIB, 20 * GIB),
                (35, 11 * GIB, 20 * GIB),
                (22, 13 * GIB, 20 * GIB),
                (22, 11 * GIB, 22 * GIB)):
            with self.subTest(target=target, used=used, minimum=minimum):
                with self.assertRaises(WindowsDiskResizeError):
                    plan_guest_layout(target, "base", used_bytes=used,
                                      supported_min_bytes=minimum)

    def test_private_base_prepare_and_verify_include_headroom_policy(self):
        prepare = build_prepare_script(22, "base", JOB,
                                       native_unencrypted=True)
        verify = build_verify_script(22, "base", JOB, phase="post_host")
        self.assertIn("$role = 'base'", prepare)
        self.assertIn("$role -eq 'base' -and $freeAfter -lt [UInt64]8589934592", prepare)
        self.assertIn("$role = 'base'", verify)
        self.assertIn("$role -eq 'base' -and $free -lt [UInt64]8589934592", verify)
        self.assertIn("$expectedDiskBytes = [UInt64]23622320128", verify)

    def test_preflight_blocks_role_headroom_or_ntfs_minimum_mismatch(self):
        for target, role, used, minimum in (
                (30, "server", 21 * GIB, 22 * GIB),
                (32, "server", 18 * GIB, 22 * GIB),
                (36, "jumpbox", 21 * GIB, 22 * GIB),
                (32, "server", 19 * GIB, 31 * GIB)):
            with self.subTest(target=target, role=role, used=used, minimum=minimum):
                with self.assertRaises(WindowsDiskResizeError):
                    plan_guest_layout(target, role, used_bytes=used,
                                      supported_min_bytes=minimum)

    def test_rejects_unapproved_targets_roles_and_job_identifiers(self):
        for args in ((29, "server", JOB), (35, "server", JOB),
                     (35, "jumpbox", JOB), (38, "server", JOB),
                     (32, "server", "../../journal")):
            with self.subTest(args=args):
                with self.assertRaises(WindowsDiskResizeError):
                    build_prepare_script(*args)
        with self.assertRaises(WindowsDiskResizeError):
            plan_guest_layout(32, "server", used_bytes=True,
                              supported_min_bytes=22 * GIB)
        with self.assertRaises(WindowsDiskResizeError):
            build_prepare_script(32, "server", JOB, native_unencrypted=1)

    def test_missing_bitlocker_provider_stays_blocked_without_compiled_native_proof(self):
        strict = build_prepare_script(32, "server", JOB)
        proven = build_prepare_script(32, "server", JOB, native_unencrypted=True)
        self.assertIn("$nativeUnencrypted = $false", strict)
        self.assertIn("$nativeUnencrypted = $true", proven)
        for script in (strict, proven):
            self.assertIn("Get-CimClass -Namespace", script)
            self.assertIn("if ($encClass)", script)
            self.assertIn("$conv.ConversionStatus -ne 0", script)
            self.assertIn("$prot.ProtectionStatus -ne 0", script)
        self.assertIn("elseif (-not $nativeUnencrypted)", strict)
        self.assertIn("elseif (-not $nativeUnencrypted)", proven)

    def test_winre_retained_copy_must_match_source_before_journaling(self):
        script = build_prepare_script(32, "server", JOB)
        source_hash = script.index("$sourceSha256 = (Get-FileHash")
        copy = script.index("Copy-Item -LiteralPath $source -Destination $winreBackup")
        compare = script.index("$script:winreSha256 -ne $sourceSha256")
        journal = script.index("Save-Journal 'backup_verified'")
        self.assertLess(source_hash, copy)
        self.assertLess(copy, compare)
        self.assertLess(compare, journal)

    def test_prepare_script_has_fixed_preflight_before_any_partition_mutation(self):
        script = build_prepare_script(32, "server", JOB)
        self.assertIn("$job = 'b89023ef-2ced-412c-9dc9-c29e6d260f9e'", script)
        self.assertIn("$jobRoot = Join-Path $journalRoot $job", script)
        self.assertIn("$disk.Size -ne [UInt64]68719476736", script)
        self.assertIn("$parts.Count -ne 4", script)
        self.assertIn("ClassName Win32_EncryptableVolume", script)
        self.assertIn("$conv.ConversionStatus -ne 0", script)
        self.assertIn("$prot.ProtectionStatus -ne 0", script)
        self.assertNotIn("Get-BitLockerVolume", script)
        self.assertIn("$targetCBytes -lt [UInt64]$supported.SizeMin", script)
        self.assertNotIn("$p.HealthStatus", script)
        disable = script.index("reagentc.exe\" /disable")
        self.assertLess(script.index("$null = Assert-InitialLayout", script.index("if ($j.State -eq 'backup_verified')")), disable)
        self.assertLess(disable, script.index("Remove-Partition -DiskNumber 0 -PartitionNumber 4"))
        self.assertIn("Get-FileHash -LiteralPath $winreBackup -Algorithm SHA256", script)
        self.assertIn("[regex]::Escape($expectedWinreLocation)", script)
        self.assertNotIn("Windows RE location:\\s+\\\\\\?\\GLOBALROOT", script)
        self.assertIn("Save-Journal 'recovery_removed'", script)
        self.assertIn("Save-Journal 'recovery_partition_created'", script)
        self.assertIn("$rv.FileSystem -eq 'RAW'", script)
        self.assertIn("$r.Guid", script)
        self.assertIn("$recoveryOffset = $targetBytes - 1048576 - $recoveryBytes", script)
        self.assertIn("Save-Journal 'complete'", script)
        self.assertIn("ReadyForHostShrink=$true", script)
        self.assertIn("WinreSha256=$script:winreSha256", script)
        self.assertIn("gpt attributes=0x8000000000000001", script)
        self.assertIn("verified full-disk", build_prepare_script.__doc__)

    def test_read_only_verifier_models_both_sides_of_host_shrink(self):
        pre = build_verify_script(32, "server", JOB)
        post = build_verify_script(32, "server", JOB, phase="post_host")
        self.assertIn("$phase = 'pre_host'", pre)
        self.assertIn("$expectedDiskBytes = [UInt64]68719476736", pre)
        self.assertIn("$phase = 'post_host'", post)
        self.assertIn("$expectedDiskBytes = [UInt64]34359738368", post)
        for script in (pre, post):
            self.assertIn("[UInt64]$disk.Size -ne $expectedDiskBytes", script)
            self.assertIn("$r.Offset -ne ($targetBytes - 1048576 - $recoveryBytes)", script)
            self.assertIn("$r.NoDefaultDriveLetter", script)
            self.assertIn("$p.Offset + $p.Size", script)
            self.assertIn("CBytes=[UInt64]$parts[2].Size", script)
            self.assertIn("RecoveryBytes=[UInt64]$r.Size", script)
            self.assertIn("[regex]::Escape($expectedWinreLocation)", script)
            self.assertNotIn("Windows RE location:\\s+\\\\\\?\\GLOBALROOT", script)
            self.assertNotIn("Resize-Partition -", script)
            self.assertNotIn("Remove-Partition -", script)
            self.assertNotIn("New-Partition -", script)
            self.assertNotIn("reagentc.exe\" /disable", script)
            self.assertNotIn("reagentc.exe\" /enable", script)
            self.assertNotIn("WriteAllText", script)
            self.assertNotIn("Move-Item", script)
        script = pre
        self.assertIn("$parts[2].Size -ne $targetCBytes", script)
        self.assertIn("$r.GptType.ToLowerInvariant() -ne $reType", script)
        self.assertIn("WinRE is not enabled at the relocated recovery partition", script)
        with self.assertRaises(WindowsDiskResizeError):
            build_verify_script(32, "server", JOB, phase="after_magic")


if __name__ == "__main__":
    unittest.main()
