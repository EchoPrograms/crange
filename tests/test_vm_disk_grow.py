import sys
from pathlib import Path
import unittest
import os
import tempfile
import base64
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import crange_vm_disk_grow as grow


def linux(**changes):
    row = {'os': 'linux', 'disk': '/dev/vda', 'disk_bytes': 32 * 1024**3,
           'table': 'gpt', 'root': '/dev/vda2', 'filesystem': 'ext4',
           'partition': 2, 'partition_bytes': 31 * 1024**3,
           'partition_start': 2048, 'partition_count': 2,
           'root_is_last': True, 'mount_options': 'rw,relatime',
           'free_bytes': 2 * 1024**3}
    row.update(changes)
    return row


def windows():
    return {'os': 'windows', 'disk_number': 0, 'disk_bytes': 32 * 1024**3,
            'partition_style': 'GPT', 'c_number': 3, 'c_offset': 290455552,
            'c_bytes': 29 * 1024**3, 'filesystem_bytes': 29 * 1024**3-4096, 'c_fs': 'NTFS', 'tail_number': 4,
            'tail_type': '{de94bba4-06d1-4d40-a16a-bfd50179d6ac}',
            'tail_size': 786*1024**2, 'winre_enabled': True,
            'winre_location_partition': 4, 'drive_r_available': True,
            'c_health': 'Healthy', 'encrypted': False,
            'free_bytes': 5 * 1024**3, 'windows_directory': 'C:\\Windows',
            'partitions': [
                {'number': 1, 'type': '{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}', 'offset': 1048576, 'size': 260*1024**2, 'letter': '', 'filesystem': 'FAT32'},
                {'number': 2, 'type': '{e3c9e316-0b5c-4db8-817d-f92df00215ae}', 'offset': 273678336, 'size': 16*1024**2, 'letter': '', 'filesystem': ''},
                {'number': 3, 'type': '{ebd0a0a2-b9e5-4433-87c0-68b6b72699c7}', 'offset': 290455552, 'size': 29*1024**3, 'letter': 'C', 'filesystem': 'NTFS'},
                {'number': 4, 'type': '{de94bba4-06d1-4d40-a16a-bfd50179d6ac}', 'offset': 31428968448, 'size': 786*1024**2, 'letter': '', 'filesystem': 'NTFS'}]}


class VmDiskGrowTests(unittest.TestCase):
 def test_linux_accepts_known_last_partition_and_rejects_ambiguous_layers(self):
    self.assertEqual(grow.validate_linux_layout(linux())['filesystem'],'ext4')
    for bad in (linux(table='msdos'), linux(root='/dev/mapper/vg-root'),
                linux(root_is_last=False), linux(mount_options='ro,relatime'),
                linux(filesystem='zfs')):
        with self.assertRaises(grow.DiskGrowError):
            grow.validate_linux_layout(bad)


 def test_debian_gpt_root_number_one_can_follow_efi_partitions_physically(self):
    measured=linux(root='/dev/sda1',partition=1,partition_count=3,root_is_last=True)
    self.assertEqual(grow.validate_linux_layout(measured)['partition'],1)
    with self.assertRaises(grow.DiskGrowError):
        grow.validate_linux_layout({**measured,'root_is_last':False})

 def test_windows_accepts_standard_efi_msr_c_and_winre_tail_only(self):
    self.assertEqual(grow.validate_windows_layout(windows())['c_number'],3)
    bad = windows()
    bad['partitions'][3]['type'] = '{11111111-1111-1111-1111-111111111111}'
    with self.assertRaisesRegex(grow.DiskGrowError,'standard WinRE'):
        grow.validate_windows_layout(bad)
    bad = windows()
    bad['partitions'].pop(1)
    with self.assertRaises(grow.DiskGrowError):
        grow.validate_windows_layout(bad)
    for key,value in (('drive_r_available',False),('encrypted',True),
                      ('c_health','Warning'),('winre_location_partition',3)):
        bad=windows();bad[key]=value
        with self.subTest(key=key),self.assertRaises(grow.DiskGrowError):
            grow.validate_windows_layout(bad)


 def test_windows_grow_script_preserves_winre_and_verifies_relocated_image(self):
    script = grow.windows_grow_script(windows(), 64)
    for token in ('Get-FileHash', 'Copy-Item', 'Remove-Partition', 'Resize-Partition',
                  'New-Partition', 'reagentc /disable', 'reagentc /enable',
                  'free_bytes=$free', 'restored WinRE hash mismatch',
                  "Save-State 'backed-up'", "Save-State 'recovery-removed'",
                  "Save-State 'c-grown'", "Save-State 'recovery-created'",
                  "Save-State 'complete'"):
        self.assertIn(token,script)
    self.assertIn('Format-Volume',script)
    self.assertIn('0x8000000000000001',script)
    self.assertIn('$cOffset',script)
    c_target=(64*1024**3-windows()['c_offset']-windows()['tail_size']-1024**2)//(1024**2)*(1024**2)
    self.assertIn(f'$cTarget=[int64]{c_target}',script)
    self.assertNotIn("Format-Volume -DriveLetter C",script)
    with self.assertRaises(grow.DiskGrowError):
        grow.windows_grow_script(windows(), 31)

 def test_windows_resume_uses_verified_guest_checkpoint_after_winre_removal(self):
    m=windows();m['disk_bytes']=64*1024**3;m['partitions'].pop();m['tail_number']=None
    m['tail_size']=0;m['tail_type']='';m['winre_enabled']=False;m['winre_location_partition']=None
    m['growth_journal']={'disk':0,'target':64*1024**3,'c_number':3,'c_offset':m['c_offset'],
        'old_c':m['c_bytes'],'c_target':(64*1024**3-m['c_offset']-786*1024**2-1024**2)//(1024**2)*(1024**2),
        'tail_number':4,'tail_bytes':786*1024**2,
        'tail_type':'{de94bba4-06d1-4d40-a16a-bfd50179d6ac}','was_enabled':True,
        'phase':'recovery-removed','winre_sha256':'a'*64}
    m['winre_backup_sha256']='a'*64
    self.assertEqual(grow.validate_windows_layout(m)['c_number'],3)
    script=grow.windows_grow_script(m,64)
    self.assertIn("$tailNo=4",script)
    self.assertIn("$wasEnabled=$true",script)
    self.assertIn("if($st.phase -eq 'recovery-removed')",script)

 def test_live_boot_order_resolves_sata_system_disk_and_rejects_ambiguity(self):
    self.assertEqual(grow.system_disk_slot({'sata0':'local:vm-128-disk-0,size=32G',
        'scsi0':'local:vm-128-disk-1,size=8G','boot':'order=sata0;ide2;net0'}),'sata0')
    self.assertEqual(grow.system_disk_slot({'sata0':'local:vm-128-disk-0,size=32G',
        'scsi0':'local:vm-128-disk-1,size=8G','boot':'order=sata0;ide2;net0'},
        ['sata0','ide2','net0'],'sata0'),'sata0')
    with self.assertRaises(grow.DiskGrowError):
        grow.system_disk_slot({'sata0':'local:vm-128-disk-0,size=32G',
            'boot':'order=sata0;net0'},['scsi0','ide2'],'scsi0')
    self.assertEqual(grow.system_disk_slot({'scsi0':'local:vm-1-disk-0,size=32G',
        'boot':'order=scsi0;net0'}),'scsi0')
    with self.assertRaises(grow.DiskGrowError):
        grow.system_disk_slot({'sata0':'local:vm-1-disk-0,size=32G',
            'scsi0':'local:vm-1-disk-1,size=8G','boot':'order=sata0;scsi0;net0'})

 def test_complete_guest_filesystems_are_verified_as_noops(self):
    target=64;bytes_=target*1024**3
    lin=linux(disk_bytes=bytes_,partition_bytes=bytes_-1024**2,
              filesystem_bytes=bytes_-2*1024**3,free_bytes=8*1024**3)
    self.assertTrue(grow.linux_already_grown(lin,target))
    win=windows();win['disk_bytes']=bytes_
    win['c_bytes']=((bytes_-win['c_offset']-win['tail_size']-1024**2)//(1024**2))*(1024**2)
    win['free_bytes']=3*1024**3
    win['filesystem_bytes']=win['c_bytes']-4096
    win['partitions'][2]['size']=win['c_bytes']
    win['partitions'][2]['size']=win['c_bytes']
    win['partitions'][3]['offset']=win['partitions'][2]['offset']+win['c_bytes']
    self.assertTrue(grow.windows_already_grown(win,target))

    target_row={'target_gb':target,'resource_key':'vm:web','guest_id':1}
    for os_name,measurement in (('linux',lin),('windows',win)):
        with self.subTest(os=os_name), patch.object(grow,'_selected_targets',return_value=(Mock(),[target_row])), \
                patch.object(grow,'_measure',return_value={**measurement,'os':os_name}) as measure, \
                patch.object(grow,'_qga') as qga:
            self.assertEqual(grow.initialize_selected(Path('/tmp/project-3'),{'project_id':3}),[
                {'resource_key':'vm:web','guest_id':1,'disk_gb':target,'filesystem_free_bytes':measurement['free_bytes']}])
            qga.assert_not_called()
            self.assertEqual(measure.call_count,2)

 def test_windows_requires_the_filesystem_to_match_its_partition(self):
    measurement=windows();measurement['filesystem_bytes']-=2*1024**3
    with self.assertRaisesRegex(grow.DiskGrowError,'capacities are inconsistent'):
        grow.validate_windows_layout(measurement)

 def test_preflight_rejects_guest_disk_larger_than_declared_target(self):
    target={'target_gb':31,'resource_key':'vm:web','guest_id':1}
    with patch.object(grow,'_selected_targets',return_value=(Mock(),[target])), \
            patch.object(grow,'_measure',return_value=linux(disk_bytes=32*1024**3)):
        with self.assertRaisesRegex(grow.DiskGrowError,'refuses to shrink'):
            grow.preflight_selected(Path('/tmp/project-3'),{'project_id':3})


 def test_linux_collector_is_fixed_and_does_not_accept_commands_from_data(self):
    command = grow.linux_inspector()
    self.assertEqual(command[:2],['/usr/bin/python3','-c'])
    self.assertIn('findmnt',command[2]);self.assertIn('lsblk',command[2])
    self.assertNotIn('eval(',command[2])


 def test_windows_collector_is_encoded_fixed_powershell(self):
    command = grow.windows_inspector()
    self.assertEqual(command[0],'powershell.exe')
    self.assertEqual(command[3],'-EncodedCommand')
    self.assertNotIn('Get-Disk',command[4])
    ps=base64.b64decode(command[4]).decode('utf-16le')
    for token in ('GetConversionStatus','HealthStatus','reagentc /info',
                  'drive_r_available','winre_location_partition'):
        self.assertIn(token,ps)

 def test_mutating_guest_command_persists_pid_and_never_resubmits(self):
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{'CRANGE_VM_DISK_GROW_JOURNAL':directory+'/jobs.json'}):
        api=Mock()
        api.request.side_effect=[{'pid':7},{'exited':True,'exitcode':0,'out-data':'done'}]
        target={'project_id':3,'resource_key':'vm:web','node':'proxmox2','guest_id':123}
        self.assertEqual(grow._qga(api,target,['fixed-command']),'done')
        self.assertEqual(grow._qga(api,target,['fixed-command']),'done')
        self.assertEqual(sum(call.args[0]=='POST' for call in api.request.call_args_list),1)
        self.assertEqual(os.stat(directory+'/jobs.json').st_mode&0o777,0o600)

 def test_uncertain_submission_is_journaled_without_retry(self):
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{'CRANGE_VM_DISK_GROW_JOURNAL':directory+'/jobs.json'}):
        api=Mock();api.request.side_effect=TimeoutError('private detail')
        target={'project_id':3,'resource_key':'vm:web','node':'proxmox2','guest_id':123}
        for _ in range(2):
            with self.assertRaisesRegex(grow.DiskGrowError,'uncertain|unresolved'):
                grow._qga(api,target,['fixed-command'])
        self.assertEqual(api.request.call_count,1)

 def test_guest_checkpoint_allows_a_new_pid_after_known_failure(self):
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{'CRANGE_VM_DISK_GROW_JOURNAL':directory+'/jobs.json'}):
        api=Mock();api.request.side_effect=[{'pid':7},{'exited':True,'exitcode':1},
                                             {'pid':8},{'exited':True,'exitcode':0,'out-data':'resumed'}]
        target={'project_id':3,'resource_key':'vm:web','node':'proxmox2','guest_id':123}
        with self.assertRaises(grow.DiskGrowError):grow._qga(api,target,['checkpointed-script'],resumable=True)
        self.assertEqual(grow._qga(api,target,['checkpointed-script'],resumable=True),'resumed')
        self.assertEqual(sum(call.args[0]=='POST' for call in api.request.call_args_list),2)


if __name__=='__main__':unittest.main()
