# Proxmox 5 staging network acceptance

Initial network record: 2026-10-06 06:22 UTC after probe cleanup; cache
evidence was added after the 08:36 UTC acceptance. The earlier staging-probe
checks and the Guac SSH check below passed for Proxmox 5 on Project 3's
`p003t254` staging VNet. This record contains no credentials or key material.

The earlier disposable-probe run verified VNet MTU 1450, controller SSH
management access, DNS, Internet egress, and isolation from other team
networks. Its command-level artifacts were not retained in this record.

For the Guac path, a declared disposable Alpine guest at `10.3.254.98/24` on
`p003t254` (gateway `10.3.254.1`) was created from the approved source image,
pinned to `proxmox5`, then removed. QEMU guest-agent execution on the existing
Guac VM (`10.60.0.10`) confirmed the route to the target via `10.60.0.1`.
With a temporary staging-router UFW rule limited to source
`10.60.0.10`, destination `10.3.254.98/32`, TCP port 22, a bounded SSH client
run from Guac reported `Connection established`, SSH protocol 2.0, an
OpenSSH server banner, key exchange, and the server's allowed authentication
methods. Client authentication methods were disabled, so the final
authentication failure was expected and no credentials were tested. The
temporary rule was deleted immediately afterward; it did not touch OPNsense.
The PVE task log does not retain a timestamp for the guest-agent command, so
the exact test time is not available; the check completed during the operation
that ended before this record was written.

Cleanup verification found no probe guest in Proxmox or Terraform state, the
Project 3 Terraform input matched its pre-test SHA-256, and the temporary
`.98/32` rule was absent. Existing staging rules remained, including the
declared `.2` SSH target and Guac RDP access.

This attests the Project 3 staging network, Guac SSH path, and local linked
image-cache path on Proxmox 5. It does not admit P5 for automatic placement;
the live policy keeps `allowed_nodes` P1-only while broader admission checks
remain pending.

## Local image-cache assessment

Assessed read-only after the network gate passed on 2026-10-06, before the
cache-preparation window. The catalog
entry `alpine-3-24-portable-20261005` has captured source identity and a
complete central artifact, with one ready replica on P1 and no replica on P5.
P5's `local-lvm` thin pool is active with 144,301,448,863 bytes available;
the image definition requests a 20 GiB disk. The fixed cache worker requires
the destination node and storage to be allowlisted. At that time, policy
allowed cache preparation only on P1/local-lvm, so a P5 preparation would be
rejected before allocation. At this read-only assessment, no cache had been created and no
policy grant had been added. The separate acceptance run below then prepared
and verified the P5 cache.

## Isolated probe bridge

The tracked `ansible/library-probes-host.yml` playbook (SHA-256
`88976db6ba575d715c921299e2c442db248f1147a383e797784808d466f6d4e3`) was
applied to P5 alone on 2026-10-06. Its resulting `crprobe1450` bridge is UP
with MTU 1450, `bridge-ports none`, and no configured IPv4 address. Linux
generated only an IPv6 link-local address (`fe80::/64`); this is expected for
the unaddressed isolated bridge. The bridge configuration is root-owned mode
0644, SHA-256
`d7a5736a169537b570f8ed82f4d26f5f90436551b4f08e08977df7a3892b358f`.

The one-host Ansible inventory and temporary `ansible-core==2.21.5` runner
were created under `/tmp/crange-p5-bridge-ansible-20261006` on P1. The exact
tracked playbook passed syntax-check and check/diff before apply. The temporary
runner was removed after verifying its ownership and playbook checksum; the
directory is absent. Any future probe attached to this bridge must keep its
NIC `link_down=1` and have no physical bridge ports, preserving isolation.

## Local cache and linked-clone acceptance

On 2026-10-06, the fixed `distribution-prepare` workflow restored the complete
central artifact for `alpine-3-24-portable-20261005` from source template 8830
on P1 and verified one local linked-clone probe on P5/local-lvm. The image
declares a 20 GiB disk; the fixed reservation covered 42 GiB for the base,
full probe writes, and transfer overhead. Before preparation, P5/local-lvm
reported 144,301,448,863 bytes free.

Cache job `cache-a06380f6e8564a179b3390f119677141` completed at
2026-10-06 08:36:14 UTC. P5 template VMID 150 is stopped, tagged for that exact
job and `crange-image`, and records `clone_modes_verified: ["linked"]`. The
disposable probe VMID 151 was removed. Its restore/clone/start/stop/destroy
provider tasks all completed successfully; the cache journal is `ready`, its
reservation is `released`, and its artifact pin is absent. The probe NIC used
`crprobe1450` with `link_down=1`; the bridge has no physical ports or IPv4
address.

The protected policy now approves P5/local-lvm and records
`nodes.proxmox5.snapshot_storages: ["local-lvm"]`, while `allowed_nodes` remains
P1-only and global ratios/enabled state are unchanged. The temporary P5
allowlist was removed after verification. The policy backup is retained on the
controller at
`/var/lib/crange-distribution/policy-backups/p5-cache-window-20261006T083450Z/`.
P5 cache/network acceptance does not by itself grant automatic placement.
