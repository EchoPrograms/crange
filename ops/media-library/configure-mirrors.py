#!/usr/bin/env python3
"""Root-only setup on each designated mirror; does not touch guest disks."""
import pathlib, socket, subprocess
NODE=socket.gethostname().split('.')[0]
PATHS={'proxmox1':'/mnt/pve/ssd-dir/cluster-media','proxmox4':'/var/lib/vz/cluster-media'}
if NODE not in PATHS: raise SystemExit('media-library: this is not a designated mirror node')
base=PATHS[NODE]
if NODE=='proxmox1' and not pathlib.Path('/mnt/pve/ssd-dir').is_mount(): raise SystemExit('media-library: SSD directory is not mounted')
for folder in ['template/iso','template/cache']: pathlib.Path(base,folder).mkdir(parents=True,exist_ok=True)
clients=' '.join(f'10.0.0.{n}(ro,sync,no_subtree_check,root_squash)' for n in range(101,107))
exports=pathlib.Path('/etc/exports.d/crange-media.exports'); exports.parent.mkdir(parents=True,exist_ok=True)
exports.write_text(base+' '+clients+'\n')
# NFSv4 exclusively avoids exposing rpcbind/mountd ports to library clients.
conf=pathlib.Path('/etc/nfs.conf.d/crange-media.conf'); conf.parent.mkdir(parents=True,exist_ok=True)
conf.write_text('[nfsd]\nvers3=n\nvers4=y\nvers4.0=y\nvers4.1=y\nvers4.2=y\n')
subprocess.run(['systemctl','enable','--now','nfs-server'],check=True)
subprocess.run(['exportfs','-ra'],check=True)
print(NODE+' exports '+base+' read-only to cluster nodes')
