#!/bin/sh
set -eu
# Run once on a cluster node after configure-mirrors.py succeeds on nodes1 and4.
pvesm add nfs media-mirror1 --server 10.0.0.101 --export /mnt/pve/ssd-dir/cluster-media --content iso,vztmpl --options vers=4.1,proto=tcp,port=2049,soft,timeo=20,retrans=2 --nodes proxmox1,proxmox2,proxmox3,proxmox4,proxmox5
pvesm add nfs media-mirror4 --server 10.0.0.104 --export /var/lib/vz/cluster-media --content iso,vztmpl --options vers=4.1,proto=tcp,port=2049,soft,timeo=20,retrans=2 --nodes proxmox1,proxmox2,proxmox3,proxmox4,proxmox5
