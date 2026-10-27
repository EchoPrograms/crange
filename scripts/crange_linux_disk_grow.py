"""Conservative planner and fixed-script builders for one Linux Btrfs grow.

This module is pre-integration only: it performs no I/O and never executes the
returned script. ``plan_linux_disk_grow`` consumes normalized native layout
evidence; ``build_linux_grow_script`` accepts only an exact measured identity
and a grow plan produced from that identity.

Required normalized measurements are documented in ``MEASUREMENT_KEYS``.
The guest must prove a single-device Btrfs root on one unencrypted GPT
partition, with no LUKS or LVM layer. Targets reserve effective used space,
measured Btrfs overhead, 16 GiB free, GPT tail space, and MiB alignment for
filesystem capacity. The partition endpoint is set to an exact sector.
"""
from __future__ import annotations

import math
import re
import shlex

GIB = 1024 ** 3
MIB = 1024 ** 2
GPT_MIN_TAIL = 34 * 1024  # standard 128-entry backup GPT table plus header
MAX_TARGET_BYTES = 64 * GIB
JUMPBOX_FREE_BYTES = 16 * GIB

MEASUREMENT_KEYS = (
    "disk_bytes", "filesystem", "filesystem_used_bytes",
    "btrfs_data_used_bytes", "reserved_overhead_bytes",
    "filesystem_offset_bytes", "filesystem_end_bytes", "partition_table",
    "tail_reserve_bytes", "single_device_root_btrfs", "btrfs_device_count",
    "encrypted", "lvm", "disk_device", "disk_guid", "partition_number",
    "root_partition_is_last", "root_partition_device", "partition_guid",
    "filesystem_uuid", "filesystem_partition_bytes", "sector_bytes",
)


class LinuxDiskGrowError(ValueError):
    """Measurements cannot support a safe, exact grow operation."""


def _int(m, key, minimum=0):
    value = m.get(key)
    if type(value) is not int or value < minimum:
        raise LinuxDiskGrowError(f"{key} must be an integer >= {minimum}")
    return value


def _uuid(value, key):
    if not isinstance(value, str) or not re.fullmatch(
            r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value):
        raise LinuxDiskGrowError(f"{key} must be a UUID")
    return value.lower()


def _device(value, key):
    if not isinstance(value, str) or not re.fullmatch(
            r"/dev/(?:sd[a-z]+|vd[a-z]+|xvd[a-z]+)[0-9]*|/dev/(?:nvme[0-9]+n[0-9]+|mmcblk[0-9]+)(?:p[0-9]+)?", value):
        raise LinuxDiskGrowError(f"{key} must be a whole-disk or partition device path")
    return value


def _validate(m):
    if not isinstance(m, dict):
        raise LinuxDiskGrowError("measurement must be an object")
    disk = _int(m, "disk_bytes", 1)
    fs = m.get("filesystem")
    if fs != "btrfs":
        raise LinuxDiskGrowError("only measured Btrfs root filesystems are supported")
    used = _int(m, "filesystem_used_bytes", 1)
    data = _int(m, "btrfs_data_used_bytes", 1)
    overhead = _int(m, "reserved_overhead_bytes", 1)
    offset = _int(m, "filesystem_offset_bytes", 1)
    end = _int(m, "filesystem_end_bytes", 1)
    part_bytes = _int(m, "filesystem_partition_bytes", 1)
    tail = _int(m, "tail_reserve_bytes", GPT_MIN_TAIL)
    sector = _int(m, "sector_bytes", 512)
    if m.get("partition_table") != "gpt":
        raise LinuxDiskGrowError("a measured GPT partition table is required")
    if tail < GPT_MIN_TAIL:
        raise LinuxDiskGrowError("measured GPT tail reserve is smaller than the backup table and header")
    if sector != 512 or tail % sector or offset % sector:
        raise LinuxDiskGrowError("this fixed grow supports sector-aligned 512-byte GPT layouts only")
    if m.get("single_device_root_btrfs") is not True or m.get("btrfs_device_count") != 1:
        raise LinuxDiskGrowError("root must be proven as single-device Btrfs")
    if m.get("root_partition_is_last") is not True:
        raise LinuxDiskGrowError("root must be proven as the final GPT partition")
    if m.get("encrypted") is not False or m.get("lvm") is not False:
        raise LinuxDiskGrowError("encrypted or LVM-backed root filesystems are unsupported")
    if end <= offset or end > disk or end - offset != part_bytes:
        raise LinuxDiskGrowError("root partition bounds do not match the measured disk")
    if used < data or used + overhead >= part_bytes:
        raise LinuxDiskGrowError("effective Btrfs use or reserved overhead is inconsistent with partition capacity")
    if disk != 32 * GIB:
        raise LinuxDiskGrowError("initial source disk must be exactly 32 GiB")
    diskdev = _device(m.get("disk_device"), "disk_device")
    partdev = _device(m.get("root_partition_device"), "root_partition_device")
    n = _int(m, "partition_number", 1)
    expected_part = diskdev + (f"p{n}" if "nvme" in diskdev or "mmcblk" in diskdev else str(n))
    if partdev != expected_part:
        raise LinuxDiskGrowError("root partition device does not match disk and partition number")
    return {
        "disk_bytes": disk, "filesystem_used_bytes": used,
        "btrfs_data_used_bytes": data, "reserved_overhead_bytes": overhead,
        "filesystem_offset_bytes": offset, "filesystem_end_bytes": end,
        "filesystem_partition_bytes": part_bytes, "tail_reserve_bytes": tail,
        "sector_bytes": sector,
        "disk_device": diskdev, "root_partition_device": partdev,
        "partition_number": n, "disk_guid": _uuid(m.get("disk_guid"), "disk_guid"),
        "partition_guid": _uuid(m.get("partition_guid"), "partition_guid"),
        "filesystem_uuid": _uuid(m.get("filesystem_uuid"), "filesystem_uuid"),
    }


def plan_linux_disk_grow(measurement):
    """Plan a jumpbox Btrfs grow; all capacities are exact integer bytes."""
    m = _validate(measurement)
    wanted_partition = m["filesystem_used_bytes"] + m["reserved_overhead_bytes"] + JUMPBOX_FREE_BYTES
    wanted_partition = math.ceil(wanted_partition / MIB) * MIB
    target_bytes = math.ceil((m["filesystem_offset_bytes"] + wanted_partition + m["tail_reserve_bytes"]) / GIB) * GIB
    # parted resizepart sets this exact sector-aligned end while retaining
    # the measured GPT tail rather than implicitly consuming it.
    target_partition_bytes = target_bytes - m["tail_reserve_bytes"] - m["filesystem_offset_bytes"]
    if target_bytes > MAX_TARGET_BYTES:
        return {"decision": "blocked", "reason": "safe jumpbox target exceeds the 64 GiB supported disk limit",
                "target_disk_bytes": None, "current_disk_bytes": m["disk_bytes"]}
    if target_partition_bytes - m["filesystem_used_bytes"] - m["reserved_overhead_bytes"] < JUMPBOX_FREE_BYTES:
        return {"decision": "blocked", "reason": "rounded target cannot provide 16 GiB usable free space",
                "target_disk_bytes": None, "current_disk_bytes": m["disk_bytes"]}
    if m["disk_bytes"] > target_bytes:
        return {"decision": "blocked", "reason": "source exceeds the calculated grow-only target",
                "target_disk_bytes": None, "current_disk_bytes": m["disk_bytes"]}
    if m["disk_bytes"] == target_bytes:
        decision = "keep"
        reason = "disk already has the calculated whole-GiB target; partition/filesystem state must be remeasured"
    else:
        decision = "grow"
        reason = "grow-only target preserves measured use, Btrfs overhead and 16 GiB free"
    return {"decision": decision, "reason": reason, "current_disk_bytes": m["disk_bytes"],
            "target_disk_bytes": target_bytes, "target_disk_gib": target_bytes // GIB,
            "target_partition_bytes": target_partition_bytes,
            "estimated_usable_free_bytes": target_partition_bytes - m["filesystem_used_bytes"] - m["reserved_overhead_bytes"],
            "requested_free_bytes": JUMPBOX_FREE_BYTES,
            "reserved_overhead_bytes": m["reserved_overhead_bytes"], "tail_reserve_bytes": m["tail_reserve_bytes"]}


def build_linux_grow_script(measurement, plan):
    """Build a fixed bash grow script bound to one measured root disk.

    The builder never executes the result. It accepts only a matching ``grow``
    plan and uses no caller-controlled command, mount path, or utility name.
    Runtime permits only the exact original disk/partition sizes or a known
    partially/completely grown state on the same GUIDs; all other states stop.
    """
    m = _validate(measurement)
    expected = plan_linux_disk_grow(measurement)
    if not isinstance(plan, dict) or plan != expected or plan.get("decision") != "grow":
        raise LinuxDiskGrowError("plan must be the exact grow plan for this measurement")
    if m["disk_bytes"] >= MAX_TARGET_BYTES:
        raise LinuxDiskGrowError("source disk is not eligible for a supported grow")
    vals = {key: shlex.quote(str(value)) for key, value in {
        "DISK": m["disk_device"], "PART": m["root_partition_device"],
        "DG": m["disk_guid"], "PG": m["partition_guid"], "FSUUID": m["filesystem_uuid"],
        "OLD": m["disk_bytes"], "OLDPART": m["filesystem_partition_bytes"],
        "TARGET": plan["target_disk_bytes"], "NEWPART": plan["target_partition_bytes"],
        "NEWEND": m["filesystem_offset_bytes"] + plan["target_partition_bytes"],
        "PN": m["partition_number"], "OFFSET": m["filesystem_offset_bytes"],
    }.items()}
    return f'''#!/bin/bash
set -Eeuo pipefail
DISK={vals['DISK']}; PART={vals['PART']}; EXPECT_DG={vals['DG']}; EXPECT_PG={vals['PG']}; EXPECT_FSUUID={vals['FSUUID']}
OLD={vals['OLD']}; OLDPART={vals['OLDPART']}; TARGET={vals['TARGET']}; NEWPART={vals['NEWPART']}; NEWEND={vals['NEWEND']}; PN={vals['PN']}; OFFSET={vals['OFFSET']}
for tool in blockdev sgdisk parted lsblk blkid findmnt btrfs udevadm df; do command -v "$tool" >/dev/null || {{ echo "missing required tool: $tool" >&2; exit 20; }}; done
root_source=$(findmnt -n -o SOURCE /); root_source=${{root_source%%\\[*}}
[[ "$root_source" == "$PART" ]] || {{ echo 'root source changed' >&2; exit 21; }}
[[ "$(findmnt -n -o FSTYPE /)" == btrfs ]] || {{ echo 'root filesystem changed' >&2; exit 22; }}
[[ "$(blkid -s UUID -o value /dev/disk/by-partuuid/$EXPECT_PG)" == "$EXPECT_FSUUID" ]] || {{ echo 'filesystem UUID changed' >&2; exit 23; }}
[[ "$(sgdisk -p "$DISK" | sed -n 's/^Disk identifier (GUID): *//p' | tr '[:upper:]' '[:lower:]')" == "$EXPECT_DG" ]] || {{ echo 'GPT disk GUID changed' >&2; exit 24; }}
[[ "$(sgdisk -i "$PN" "$DISK" | sed -n 's/^Partition unique GUID: *//p' | tr '[:upper:]' '[:lower:]')" == "$EXPECT_PG" ]] || {{ echo 'root partition GUID changed' >&2; exit 25; }}
[[ "$(lsblk -dn -o TYPE "$DISK")" == disk && "$(lsblk -dn -o TYPE "$PART")" == part ]] || {{ echo 'device topology changed' >&2; exit 26; }}
[[ "$(sgdisk -p "$DISK" | awk '$1 ~ /^[0-9]+$/ {{n=$1}} END {{print n+0}}')" == "$PN" ]] || {{ echo 'root partition is no longer final' >&2; exit 32; }}
sector=$(blockdev --getss "$DISK"); start=$(lsblk -bndo START "$PART")
[[ "$((start * sector))" == "$OFFSET" ]] || {{ echo 'root partition start changed' >&2; exit 33; }}
[[ "$(btrfs filesystem show --raw / | grep -c ' devid ')" -eq 1 ]] || {{ echo 'root Btrfs is no longer single-device' >&2; exit 27; }}
current=$(blockdev --getsize64 "$DISK"); partbytes=$(blockdev --getsize64 "$PART")
[[ "$current" == "$OLD" || "$current" == "$TARGET" ]] || {{ echo 'disk size is neither original nor exact target' >&2; exit 28; }}
[[ "$current" == "$TARGET" ]] || {{ echo 'host disk growth to the exact target is required before guest partition writes' >&2; exit 34; }}
[[ "$partbytes" == "$OLDPART" || "$partbytes" == "$NEWPART" ]] || {{ echo 'partition size is not a known resumable state' >&2; exit 29; }}
if [[ "$partbytes" == "$OLDPART" ]]; then
  sgdisk -e "$DISK"
  parted -s "$DISK" unit B resizepart "$PN" "${{NEWEND}}B"
  udevadm settle
fi
[[ "$(blockdev --getsize64 "$DISK")" == "$TARGET" && "$(blockdev --getsize64 "$PART")" == "$NEWPART" ]] || {{ echo 'grown disk layout does not match exact target' >&2; exit 31; }}
btrfs filesystem resize max /
btrfs filesystem usage -b /
available=$(df -B1 --output=avail / | tail -n 1 | tr -d '[:space:]')
[[ "$available" =~ ^[0-9]+$ && "$available" -ge {JUMPBOX_FREE_BYTES} ]] || {{ echo 'fresh filesystem measurement does not prove 16 GiB available' >&2; exit 35; }}
printf 'grow_verified disk_bytes=%s partition_bytes=%s available_bytes=%s\\n' "$TARGET" "$NEWPART" "$available"
'''
