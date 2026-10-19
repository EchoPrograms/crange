# Proxmox 6 enrollment — 2026-10-15

Requested after the administrator joined the node to the live cluster.

## Verified host preparation

- Cluster: all six members online, quorate, six votes / four-vote quorum.
- Management: `10.0.0.106/24`, gateway `10.0.0.1`; existing `vmbr0` configuration preserved.
- Hardware: two Xeon E5-2697 v2 CPUs, 24 physical cores / 48 logical CPUs,
  approximately 378 GiB RAM. CPU flags support the range's `x86-64-v2-AES` baseline.
- `local-lvm`: approximately 612 GiB empty thin pool. Native 64 MiB thin-volume
  and snapshot creation/removal passed; disposable proof volumes removed.
- `local-hdd`: originally an entirely empty 4.37 TiB thick LVM volume group.
  Administrator explicitly requested thin conversion. Confirmed zero LVs,
  content and guest/snapshot references before creation. Created
  `local-hdd/crange_data`, 4,016.01 GiB, with 4 GiB metadata, 256 KiB chunks and
  monitoring enabled; approximately 10% of VG remains free. Actual 64 MiB
  parent/snapshot write divergence verified native copy-on-write; both proof
  volumes removed. Retained the `local-hdd` storage ID, now type `lvmthin`,
  limited to Proxmox6. No guest disks or VGs were erased.
  Root receipt: `/var/lib/crange-storage/p6-hdd-67b22645fc6b41ac84572635a9f2393f/receipt.json`.
  Range policy approves it alongside local-lvm; fresh VM and CT linked-clone
  acceptance passed on both pools.
- Existing range VXLAN zone extended with node 6 and peer `10.0.0.106`.
  Existing project VNets appeared on the new node at MTU 1450.
- `crange-library`, `media-mirror1`, and `media-mirror4` storage registrations
  include node 6. Mirror exports allow its management IP read-only/root-squashed.
  The library Ganesha ACL and persistent/runtime nftables client set include it.
  Existing NFS connections were retained; Ganesha configuration was reloaded.
- Installed the pinned private bridge guard and persistent addressless
  `crprobe1450` bridge. Host/routed traffic drop rules and boot persistence passed.
  Dashboard preparation job `c6c5dcaa9ca546c880c12796b70ef6c4` succeeded.
- Node SSH uses the cluster-managed `/etc/pve/nodes/proxmox6/ssh_known_hosts`.
  The root relay gained only its fixed `10.0.0.106` target.

## Automation and acceptance

Node validation, network verification, cache reconciliation, Windows seed cleanup,
console access, provider compatibility, Cluster settings and GUI node suggestions
now recognize nodes 1–6. Unknown nodes remain rejected. Existing per-node storage,
provider and exact-LAN acceptance gates remain enforced.

PVE 9.0.3 on node 6 lacks the provider's `host-managed` CT network property.
Its actual schema was measured and added to the existing loopback compatibility
adapter. Only explicit false values are removed, as on other measured 9.0.3 nodes.

A fresh disposable Debian 12 CT was created with pinned provider `0.104.0` on
`local-lvm`, stopped on the isolated bridge. Saved create/delete plans each
mutated exactly that CT (900006). Network configuration readback passed and the
CT was deleted. Evidence: controller
`~/.local/share/crange/node-acceptance/p6-20261015/receipt.json`.
Create-plan SHA-256:
`edcef77b5363800b8ce9ef5c27e9ac315edc0d630595a6ce916133e39337c184`.

Candidate policy enrollment does not assert a network attestation. Each project
LAN still needs the normal authenticated `network-verify` job and published
source-bound receipt before automatic placement can use it.

## Findings and recovery history

The first generic Alpine VM cache restore returned HTTP 403: the immutable
archive retains source VMID 8830, whose exact `VM.Backup` permission was absent.
Proxmox checks that permission when reading backup archives. Added only the
existing backup-only role on `/vms/8830` for worker user/token; no host administrator
role was granted. Exact failed cache journal `cache-617dff2ac7304c39a1eb3f61699fcc38`
was reconciled with complete guest/task absence proof before retrying.

VM cache retry: dashboard job `0dc05d9d879349e69b3dd617a62c339a`.
VM cache retry succeeded: immutable Alpine base VM153 on Proxmox6/local-lvm.
Its independent linked probe VM154 passed identity/readiness/isolation and was
stopped/deleted; exact provider tasks are terminal OK.

Container cache acceptance job `ea032aa9f9904f55abb89a2a0cee5b6c` succeeded.
Retained Alpine CT template154/local-lvm passed restore, linked clone, physical
LVM origin, independent identity/readiness, and probe cleanup. Receipt journal
SHA-256: `d4cc94be8a88152f84db6d70c88e8009a37bd4e039d751b34f4f677ff38f4135`.
Its archive source VMID 8840 received the same exact backup-only permission.
Fixed the administrator container acceptance schema to accept published image IDs
up to 128 characters, matching ordinary image preparation.

Queued normal dashboard network verification for all existing Project 100 LANs:

| LAN | Job |
| --- | --- |
| Staging (`p100t254`) | `210d2d32e35e49b3ae5fbb78eb95d7b6` |
| `p100t001` | `53b6221fca0f43af8cacc205aa1563ef` |
| `p100t002` | `de6df10c62954b28bffd596e4888fa37` |
| `p100t003` | `e415ca35a0cb417ba645f9ea0adb10d0` |
| `p100t004` | `61756225148e49a98b779a46d535c5ac` |
| `p100t005` | `0d0874f412374c22a3cbe9b62e10eade` |

Browser acceptance confirms all six nodes visible in Cluster, capacity and
commitment limits, GB storage units, and zero compute commitment for 42 observed
stopped/template rows. Library read/write/fsync/readback and temporary-file
cleanup passed from node 6. Its management address remains `10.0.0.106/24`.

All six network jobs succeeded; final results are recorded below.

## Lab capture status at enrollment

Release capture `9401509de97b4c5cb92b9308889a8ecd` had already failed safely on
remaining full-copy disk headroom before enrollment. It accepted the FS image
and APP CT fresh-clone verification. BKP capture was refused before allocation;
the source was restarted. Adding node 6 does not automatically restart that job
or alter existing release checkpoints, guests, project inputs, or reservations.


## Network admission completed

All six listed verification jobs succeeded and published their exact LAN
receipts. Controller, Guacamole, DNS, internet egress, isolation and MTU checks
passed in each LAN; owned probes, temporary rules, Terraform state and capacity
reservations were cleaned. See `proxmox6-network-acceptance.json`.
Read-only scheduling also selected Proxmox6 using an automatic soft preference
on the accepted staging network, without creating a guest or reservation.

## HDD range integration

The worker user and token have only the standard datastore audit/space role on
`/storage/local-hdd`. Pool admission retains native snapshot proof and all host
reserves, capacity ratios, exact-LAN checks and per-image clone verification.
The fixed CT backing relay permits this node/pool only when its live storage
config remains node-6-only, non-shared `lvmthin`, VG `local-hdd`, thinpool
`crange_data`; backing origin is still checked physically.

Added local-hdd to typed job/CLI/capture storage options and the admin GUI.
Corrected the shared form renderer so maintenance node selectors remain explicit;
new scenario resources still use Automatic placement with suggested nodes.
Browser tests verified six-node Cluster visibility and the node/storage fields.

Fresh HDD VM preparation job: `bdd9c67403a542549f0289a2cc19c0be`.
Fresh HDD CT cache acceptance job: `d2834353330143d9b804209e77d6f4d3`.


## HDD acceptance completed

- VM template155: immutable archive restore, fresh linked clone, identity and
  readiness verification, stop/delete cleanup passed. The final global inventory
  read hit a transient Proxmox4 HTTP595 after the cache was already published;
  the original failed dashboard job remains in audit history. Exact existing
  cache `cache-7befd6f018094f42ade701976315510c` was reconciled successfully,
  releasing only its temporary capacity reservation and archive pin. No restore
  or probe was repeated.
- CT template156: HDD acceptance job `d2834353330143d9b804209e77d6f4d3`
  succeeded. Physical LVM snapshot origin, unique identity, readiness and probe
  cleanup passed. Receipt journal SHA-256:
  `f272cbab75b498cbdf00d42722ce32037aa91b42a5ed85409a847d86fc4fd4bc`.
- Reconciliation now handles VMID reuse across guest kinds only with a fresh
  successful exact original destroy-task receipt. Same-kind/unknown guests and
  uncertain deletion stay blocked. No replacement guest is modified or deleted.
  Three reuse/denial tests, ten cache-job tests and 47 distribution tests passed.
- All temporary native proof volumes and disposable guests were removed. Kept
  only the approved immutable VM/CT cache templates needed for new placements.


Final read-only scheduling selected Proxmox6/local-hdd for both a VM and a
container using Automatic plus a soft node suggestion, without pinning storage
or creating guests/reservations. Both HDD cache reservations are released.
Evidence: `proxmox6-placement-acceptance.json`.

Returned to lab capture through dashboard job
`d716569816bd4fb5a02772dcfcf43a17` after completing enrollment. This resumes
release 0.1.1 checkpoints; participant rollout remains pending.
