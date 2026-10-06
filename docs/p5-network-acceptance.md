# Proxmox 5 staging network acceptance

Recorded 2026-10-06 06:22 UTC after probe cleanup. The earlier staging-probe
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

This attests the Project 3 staging network and Guac SSH path for Proxmox 5. It
does not attest local image-cache availability, snapshot/restore behavior, or
P5 placement admission. Keep `allowed_nodes` P1-only until the separate P5
image-cache and snapshot acceptance gates pass.

## Local image-cache assessment

Assessed read-only after the network gate passed on 2026-10-06. The catalog
entry `alpine-3-24-portable-20261005` has captured source identity and a
complete central artifact, with one ready replica on P1 and no replica on P5.
P5's `local-lvm` thin pool is active with 144,301,448,863 bytes available;
the image definition requests a 20 GiB disk. The fixed cache worker requires
the destination node and storage to be allowlisted. Current policy approves
only P1 and P1/local-lvm, so a P5 cache preparation would be rejected before
allocation. No cache was created and no policy grant was added. P5 local
linked-clone and snapshot/restore acceptance remain pending.

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
