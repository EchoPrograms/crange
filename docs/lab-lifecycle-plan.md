# Lab lifecycle completion and template library

The current objective is to finish the lab lifecycle, verify it on the range,
then build and validate approved operating-system templates. This is separate
from the completed Guacamole customization/capability audit.

## Lifecycle work

- Individual scenario VM/container Start and graceful Stop: implemented as
  fixed `machine-state` jobs, with applied-ID/name/template checks before power
  operations and desired state saved after the requested state is confirmed.
  Exposed beside each guest in Scenario and Teams. Source tests cover both kinds
  and directions, rejecting unknown options and identity mismatches. A live
  idempotent start of staging VM `t254-web-01` confirmed the applied identity
  without restarting it. No other guest was operated on.
- Preserve custom topology across capture, fresh-clone testing, and rollout.
  Include routers, network allocation, guest attachments, and stable addresses;
  ordinary team deployments must remain unchanged by staging edits.
- Support explicit selected-team upgrade and rollback to immutable releases,
  removing/replacing only previously owned release resources after reviewed
  plans. Record per-team release/rollout history and health outcomes.
- Complete VM/container edit controls and show authoritative guest status.
- Run an isolated VM/container/custom-network acceptance scenario through
  capture, fresh-clone test, selected-team rollout, Guacamole access, revocation,
  and rollback. Verify that an unselected team's resources stay unchanged.

## Template work after lifecycle acceptance

Build Debian, Ubuntu Server, and Alpine as VM and Linux container bases; build
OPNsense as a VM. OPNsense uses FreeBSD and is not an LXC container base. Retain
the existing Parrot jump-box image. Additional server/security/router variants
can be added once the requested bases pass clone, addressing, boot, and
management-access tests.

Use official image sources, verify published checksums/signatures, record
versions/checksums and reproducible build steps, allocate unused VM/CT IDs,
keep templates in the range pool, and register only verified images in approved
allowlists. Never replace unrelated guests or the existing shared range firewall.
Build tests must cover a fresh clone, static network customization, SSH/console,
graceful power operations, guest-agent behavior where supported, and release
capture compatibility. OPNsense needs a VM-specific installation/configuration
path rather than assuming Linux cloud-init and systemd.

Official source references:

- [Debian cloud images](https://cloud.debian.org/images/cloud/)
- [Ubuntu cloud-image artifacts and verification](https://ubuntu.com/docs/public-images/public-images-reference/artifacts/)
- [Alpine cloud images](https://alpinelinux.org/cloud/)
- [OPNsense virtual installation](https://docs.opnsense.org/manual/virtuals.html)
- [OPNsense installation/checksum verification](https://docs.opnsense.org/manual/install.html)

The initial live inventory contained VM template 112 (Parrot jump box) and
VM template 116 (a Project 3 captured release image). No Linux container bases
were present in the inspected cluster guest inventory. The requested new bases
have not yet been built.

## VM network placement progress (2026-09-30)

VMs now support the same owner-scoped custom network selection as containers,
with standard staging placement as the default. VM edit exposes the static host
octet and subnet; project/team ownership is inferred and cannot be changed by
this form. Selecting a subnet during creation chooses a free address in that
subnet. Network options display CIDRs. Terraform's scenario VM module now accepts
an optional subnet ID/VNet and computes the guest address/gateway/name/tags from
that placement. Existing standard-team defaults retain their previous values.

Validation checks VM/CT/jump-box address collisions in the selected subnet,
rejects networks owned by another team, preserves parent router uplink
reservations, updates VM placement when a network is renumbered, and refuses to
remove a network that still has VMs attached. Failed edits do not persist desired
state. The CLI and fixed GUI job schemas carry these options through to Terraform.

All 80 source tests pass. Both Project 3 and the clean project template pass
Terraform validation. A live read-only Project 3 plan still showed the same
pending staging VM create/update and container create, with no new replacements
or ordinary-team changes; no apply occurred. Browser verification against the
deployed assets with a controlled project-response fixture confirmed current
VM IP/subnet prefill, owner inference, and mobile layout. Temporary verification
accounts and the unused read-only plan were removed.

This completes the VM placement/edit prerequisite. Capturing, allocating, and
cloning custom topology in immutable releases is still next; the current release
capture path must not be treated as supporting custom subnets yet. No new OS
bases have been registered during this step.

## Release topology source implementation (2026-09-30)

Schema 4 captures staging custom-router declarations and VM/container network
attachments, verifies their applied addresses and router outputs, and rejects
capture while disposable release tests remain declared. Test preparation allocates
separate project subnet IDs and parent-router addresses for captured networks;
custom guests retain their host octets. Standard staging test clones share the
reserved .90–.100 allocation across VM and container kinds, excluding existing
static guests, jump boxes, and router uplinks. Selected-team release declarations
allocate a distinct copy of the captured topology for each team.

Test cleanup verifies recorded release ownership and refuses to remove changed
network definitions or networks borrowed by other guests. Test boot-order checks
verify each applied guest's name, template flag, staging owner, release ownership,
and address before any power action. Legacy standard-network manifests remain
supported; a guest referencing an uncaptured custom network is refused rather
than silently moved into the standard LAN.

The source suite has 88 passing tests, including topology allocation, VM/container
attachments, staging preservation, safe cleanup, schema 4 capture, and stale-ID
power-action rejection. This is source verification; fresh-clone live topology
acceptance, upgrade/rollback, and the requested base OS templates remain pending.
No infrastructure apply was performed for this source change.

The controller source suite also passes. Its read-only Project 3 plan shows only
existing pending staging declarations: VM `t254-test-vm` create, VM
`t254-web-01` update, and CT `t254-example-name` create. No ordinary-team
resource changes, destroys, replacements, or staging-scope blockers appeared.
The verification plan was removed without applying it.

## Selected-team upgrade, rollback and rollout health (2026-09-30)

`release-deploy` now records owned release declarations and per-team history.
`release-upgrade PROJECT VERSION --teams ... --from-version CURRENT` replaces
only the current recorded release's VM/container/network declarations.
`release-rollback` uses the same inputs and additionally requires the target
version to have an infrastructure-applied event for each selected team. These
commands only edit desired state. Stale expected versions, unrecorded release
resources, changed ownership/declarations, and networks with other guests
attached are refused without writing the changed configuration.

Generate and inspect `infra-plan PROJECT --retain`, then use
`release-apply PROJECT --teams ... --plan-id ID --yes` with the explicit planned
delete/replacement approvals where needed. Release apply checks the actual
Terraform plan against both the previously applied and newly declared owned
resources. It refuses staging, unselected-team and unowned changes, then applies,
configures only the selected teams and runs their release health checks. Shared
SDN application resources and project-router return-route maintenance are the
limited shared operations. Unrelated pending project changes must be resolved
separately before a release-only plan can be applied.

Old applied subnet IDs/uplinks stay reserved during a transition so Terraform
cannot attempt to create and delete the same physical VNet under different
resource keys. Captured images and older immutable manifests remain available.
History distinguishes declarations, infrastructure application, health checks
and detected drift. `release-check PROJECT --teams ...` verifies applied guest
addresses, project names, non-template identities, running status and every
configured health check; it records failures as well as passes without power
operations. A failed deployment can be rolled back after its infrastructure has
been applied; declaration alone does not prove deployment or healthy services.

The Scenario GUI now offers Stage release, Stage upgrade, Stage rollback,
Apply reviewed release and Check deployed release. The current source version
for upgrade/rollback is inferred from selected teams rather than exposed as an
editable field. Teams selected together must share a recorded current version.
The release table shows declared/applied versions and health, with per-team
history expandable below it. All operations use fixed, project-authorized jobs
and the existing persistent job output dialog.

The source suite has 100 passing tests. Tests cover ownership/drift refusal,
expected-version races, real VM/container declaration transitions, old-VNet
reservation, rollback history, preservation of staging and an unselected team,
health failure/recovery, selected-team configuration and actual-plan scope
checking before Terraform apply. Full live lifecycle acceptance and the requested
OS template library are still outstanding.

Controller verification: all 100 tests pass; both Project 3 and the clean template
pass Terraform formatting and validation. The live read-only plan still contains
only the previously pending staging VM create/update and staging container create.
The release plan classifier rejects all three changes for a Team 1 release apply,
while the staging classifier accepts their scope. The unused verification plan
was removed; no infrastructure apply occurred.

Browser verification against the deployed assets used a temporary manager and
controlled project/job responses. It confirmed release history, inferred current
versions, upgrade/rollback job payloads, invalidation of an old plan after staging
release changes, selection of a fresh reviewed plan, the closable job dialog, and
mobile layout. These fixture jobs were not sent to the real worker. Temporary
accounts were removed. The web and worker services were reloaded only while the
worker had no running job.

## Verified Debian container base and isolated lifecycle project (2026-09-30)

The administrator-only container builder verifies the archive SHA512 from the
signed Proxmox catalog before creating an unprivileged base. Debian 13 template
CT 3000 was built on proxmox4 and tested using two fresh DHCP clones and a
separate static-address clone. SSH, controller sudo, distinct machine IDs and
host keys, and graceful shutdown passed. All three temporary clones were
removed. The approved Debian container alias now references this actual LXC
template instead of VM 120; VM 120 remains unchanged. Build details and exact
archive identity are recorded in `templates/container-debian-13.json`.

Saved plans now fingerprint template approvals and Terraform sources as well as
desired state. Changed approvals/modules invalidate old plans before apply.
Project router configuration now checks a fixed, desired-state-derived OPNsense
static route and gateway; it never changes firewall policy. Matching routes need
no writes. Conflicting unmanaged and duplicate routes are refused before writes.

A new-project plan exposed an invalid replacement trigger when the custom VNet
map was empty. An always-present `terraform_data.sdn_revision` now records VNet
changes and triggers SDN application. Existing projects require a reviewed
bookkeeping migration plan before their next apply. Project 3 has not been
applied as part of these changes.

All 107 source tests pass on the controller. Project 240 uses its own state and a
project-scoped temporary verification manager. Its retained plan was inspected:
11 creates, no updates, deletions or replacements. The real worker applied four
routers and four VNets plus SDN bookkeeping successfully. Router configuration,
VM/container capture, fresh-clone testing, selected-team upgrades/rollback and
the remaining OS template library are not yet fully verified.

Fresh-project configuration exposed two bootstrap assumptions missed by the
existing-project tests. OPNsense's route model can lag a newly saved gateway;
the helper now polls the route gateway choices for up to 30 seconds before a
write. Project 240's managed aggregate route was subsequently created. New
Debian router archives do not include UFW or dnsmasq. Router configuration now
installs UFW on all routers and dnsmasq on team, staging and custom routers.
These changes passed the controller's Ansible syntax check; full live router
configuration verification is in progress.

Empty Guacamole SSH target lists now produce a valid newline record instead of
empty template content rejected by Ansible copy. Both Guacamole target and
custom-route record parsing discard blank lines, preventing attempts to remove
a firewall rule for an empty address. The controller source suite passes 108
tests. The live configuration retry is tracked in the worker audit; a transient
team-router SSH failure was checked directly and the proxy SSH command succeeded
before retrying the terminal failed job.

The repeated team-router SSH failure was reproduced with the real Ansible
`setup` module: pipelining failed, while normal module transfer succeeded. A
small pipelined `ping` module and direct SSH both succeeded. Generated inventory
now disables pipelining only for team, staging and custom routers reached through
SSH ProxyCommand, leaving directly reached hosts unchanged. The complete live
configuration job remains the acceptance gate for this transport change.

## Live staging VM/container/custom-subnet verification (2026-09-30)

Project 240's complete initial router configuration succeeded through the actual
worker. All four routers have the expected routes, active UFW, IPv4 forwarding,
and working DNS. A second retained plan was inspected and applied through
`stage-deploy`: custom staging subnet `10.240.100.0/24`, its router CT 128,
Debian CT 129 at `.21` and VM 130 at `.20`. The only replacement was the SDN
applier bookkeeping resource. No ordinary-team or existing guest resource
changed. The same job configured the staging path and created two Guacamole
connections successfully.

The container resolved DNS and fetched an HTTPS response from deb.debian.org.
Attempts to connect to Team 1's router SSH and the controller web service timed
out. Its default ping health check passed. The VM's SSH service was accessible
from the controller, but the parent staging-router health check was blocked by
custom-subnet isolation. Health checks now select the applied router on the
guest's own subnet, without adding firewall exceptions. Missing, ambiguous,
foreign-project and other-team custom router selection is refused. Both the
VM TCP:22 and container ping checks subsequently passed in real worker jobs.
The controller suite passes 109 tests.

Release 1.0.0 capture is running with both `desk,svc` and `svc,desk` boot-order
permutations. Fresh-clone release tests, selected-team rollout, upgrade/rollback,
Guacamole native login acceptance and the remaining OS templates are still
outstanding.

The first mixed VM/container capture created its two images and restarted both
source guests, then failed because the LXC template endpoint returns null rather
than the background worker ID. Conversion now waits for the actual template flag
and supports a task ID if supplied by another endpoint version. Invalid task IDs
produce a CRANGE error rather than an incidental URL-encoding exception.
Cleanup also exposed DELETE options incorrectly sent as a request body, which
Proxmox rejects with HTTP 501. GET/DELETE options now use query parameters;
POST/PUT retain their request bodies. These contracts are covered by regression
tests and the controller suite passes 113 tests.

The two images from the failed attempt (131,132) were verified as stopped,
unpublished Project 240 capture images, then deleted through the corrected API
client. Cleanup was audited against the failed job. Capture has been retried via
the worker. These fixes follow the primary Proxmox LXC API and HTTP server:
https://github.com/proxmox/pve-container/blob/master/src/PVE/API2/LXC.pm
https://github.com/proxmox/pve-http-server/blob/master/src/PVE/APIServer/AnyEvent.pm

Capture retry succeeded: immutable schema-4 release `p240-1.0.0` includes VM
image 131, container image 132, the custom network, both health checks and both
boot orders. Neither image is deployed to ordinary teams yet. Fresh-clone
preparation initially refused the captured VM because the manifest includes
`kind` while approvals store only ID/node. VM and container references now compare
the approved ID/node and separately validate their kind. The regression test
captures a scenario and directly prepares its written manifest, avoiding the
older fixture shape that hid this mismatch. All 113 tests pass.

The real `release-test-prepare` retry succeeded and declared one VM, one container
and a separate test subnet while preserving the original staging declarations.
A retained Terraform plan is being generated for the fresh test resources.
The clone health/boot permutations, identity isolation, selected-team rollout,
upgrade/rollback and Guacamole login gates remain outstanding.

Fresh-clone planning exposed a Terraform uniqueness check that still keyed VM
addresses by owner team, unlike CTs and jump boxes. It falsely collided the
source VM at `10.240.100.20` with its test clone at `10.240.101.20`. VM addresses
now use the actual custom subnet ID in the combined uniqueness validation.
Three native Terraform plan tests with a mocked Proxmox provider pass: repeated
VM/CT host octets on separate subnets are allowed, while VM/CT and VM/VM collisions
on the same subnet are rejected. These tests do not contact or modify Proxmox.
The corrected real Project 240 fresh-clone plan has been queued for review.

## Fresh-clone container identities (2026-09-30)

The reviewed Project 240 fresh-clone deployment succeeded: disposable VM 135,
container 134, router 133 and their separate subnet were created and configured.
Source staging VM 130 also received the reviewed primary-disk-first boot-order
correction; no ordinary-team guest changed.

Live identity comparison exposed inherited SSH host keys in captured containers,
although Proxmox gave their clones distinct machine IDs. Container configuration
now installs a fixed identity helper and a startup service before SSH. A changed
machine ID, hostname or MAC regenerates all host-key algorithms; repeated
configuration preserves them. Systemd and Alpine OpenRC startup paths are
provided. Alpine's path remains pending live template verification. New container
base builds include Python 3 for this reviewed configuration.

The first live key rotation succeeded, but SSH's reload left both verification
containers' daemons stopped. Their services were restored through Proxmox,
audited, and the handler now uses restart. The subsequent worker configuration
succeeded with zero changes on both guests. Independent inspection confirmed
distinct machine IDs and every SSH public-key fingerprint, with SSH active on
CTs 129 and 134. No private keys were copied to the audit or repository.

The controller suite passes 116 tests, including real temporary SSH-key
generation and capture/clone identity changes. Team configuration includes only
the selected team's container inventory group; a regression test covers staging
and Team 1 while excluding Team 2. Project-wide box configuration remains an
explicit project-wide operation.

Release 1.0.0's actual fresh-clone boot-order/health test is running through the
worker. Reboot identity persistence, selected-team rollout, upgrade/rollback,
Guacamole login gates and the remaining OS template library are not yet complete.

The first live permutation run found a startup race: the VM's 10-second TCP
probe failed just after boot but passed later in the same run. Release boot tests
now retry configured probes for a bounded 180-second startup-readiness window.
Individual probe settings are preserved; a service that never becomes ready
still fails. Ordinary manual health checks remain immediate. Two regression
tests cover transient readiness and persistent failure; all 118 controller
tests pass.

The real retry succeeded for both captured VM/container boot permutations,
recording `1.0.0-20260930T133441Z.json` as passed. After those stop/start cycles,
CT 134 retained its previously generated host keys and SSH was active. Controller
SSH scans confirmed distinct RSA, ECDSA and Ed25519 served keys for source 129
and clone 134. This verifies identity separation and reboot persistence on the
Debian capture path. Disposable clone cleanup and selected-team rollout are
the next live acceptance steps.

## Selected-team rollout acceptance (2026-09-30)

Disposable test cleanup completed through the reviewed staging deployment job.
The plan deleted only owned router CT 133, CT 134, VM 135 and VNet p240c101,
plus replacing SDN apply bookkeeping. Physical inventory confirmed those guests
absent, source staging guests still running and captured images retained. Router
configuration completed and Guacamole reconciliation removed the two test
connections, leaving only the staging source connections.

Release 1.0.0 was explicitly declared for Team 1 alone. The retained plan was
inspected as JSON and passed selected-release ownership checks: create a new
Team 1 custom router/subnet and VM/container clones from images 131/132, with no
staging or Team 2 guest changes. Team 2 router configuration was fingerprinted
before rollout. The actual release-apply worker job is running; applied health,
unselected-team preservation and native Guacamole access are not yet claimed.

The selected-team release application succeeded. Team 1 VM 135 and container
134 were created on custom subnet 101 behind router 133, configured, and passed
their captured TCP:22 and ping checks. The container SSH restart handler
successfully activated newly generated keys. Team 2 router's Proxmox config
fingerprint still matches its pre-rollout value; it has no release declaration.
The worker configured only Team 1/custom router and its container, with shared
project return routes, and reconciled four Guacamole connections total.

An actual project Users API job created a temporary separate Guacamole account
with only the two Team 1 release connections. Native Guacamole login returned
exactly those connection IDs. A second job revoked its grants and fresh login
returned no connections. Account removal then denied native login; the same
credentials also cannot log into CRANGE, and their password was absent from all
Project 240 job output. This verifies login/grant reconciliation and revocation;
a full Guacamole remote terminal session on these new release guests remains
to be exercised. Temporary account cleanup completed.

## Second-release fresh-clone acceptance (2026-09-30)

Release `p240-1.1.0` was captured as immutable VM/CT images 136/137.
Its isolated test subnet 102, router 138, VM 139 and container 140 were
deployed and configured through reviewed automation. The staging guard
initially refused inherited boot-order drift on Team 1 VM 135. A separately
reviewed targeted plan corrected only that VM’s boot metadata and created its
new test network/router dependencies; no live guest was deleted or replaced.
The regenerated staging plan created only the two disposable test guests.

The actual release test passed VM TCP:22 and container ping after the recorded
boot order, with result `1.1.0-20260930T182255Z.json`. Team 2 router configuration
still matches SHA-256
`7803fb76b7cf42f9f9387a0bcf04d914f600c009266efd7a8f97ae0aefcf33b2`.
Test cleanup declarations are being prepared for reviewed removal. Live
selected-team upgrade and rollback, a full Guacamole remote session, and the
remaining OS bases are still outstanding.

## Linux VM base acceptance (2026-09-30)

Ubuntu Server 24.04 VM template 3014 and Debian 13 template 3015 passed
fresh-clone verification: separate SSH keys/machine identities, controller
SSH/sudo, bootstrap password login, static IPv4, DNS/HTTPS egress, graceful
shutdown and persistence across reboot. Cloud-init emitted only the recorded
Proxmox deprecated string-user advisory; these checks do not claim absence
of warnings. Both template seeds disable automatic first-boot package upgrades.

Per-image hardware profiles now select q35 and the guest agent for these bases,
while retaining legacy defaults on existing guests. Captures pin the effective
profile and disable package upgrades on release clones. Display labels no
longer alter release image identity. All 136 controller unit tests and Terraform
validation for Project 3, Project 240 and the clean project template pass.

The two bases are approved only in isolated Project 240 pending workflow
acceptance. Staging declarations use .30 and .31. The reviewed plan
33da3e1f2374d471c6ddbefe2c4bf5bf creates exactly these two VMs, with no
updates/deletions. A stale CD-ROM fallback in test Team 1 VM 135's boot order
was corrected separately without restarting its running process. Deployment
job b772b522a0f245558ef3bf098f609696 is still running; QEMU guest agents report
VM 138 at 10.240.254.31 and VM 139 at 10.240.254.30. Applied configuration,
health and fresh captured-release acceptance remain to be checked.

The earlier selected Team 1 upgrade job a356249ad06d45008c678016018d610a
is confirmed succeeded, including VM/container health and Guacamole refresh.
Rollback acceptance remains outstanding. Alpine's official 3.24.2 generic
NoCloud image has been downloaded, SHA-512 verified and inspected; it has
not yet been customized, created as a VM template, or approved. Custom jump
boxes and OPNsense VM support remain required work.

## Additional base and release acceptance (2026-10-01 UTC)

Fresh captured release 1.2.0 deployment succeeded in Project 240. Its three
VMs (Debian, Ubuntu and Parrot) and Debian container passed the recorded boot
order and health probes, with result 1.2.0-20261001T014917Z.json. Debian/Ubuntu
VM references are now also available in Project 3 and the clean project catalog;
no Project 3 infrastructure was applied. Test cleanup declarations have been
queued; reviewed physical removal remains outstanding.

Alpine VM 3017 passed two fresh-clone identity/SSH/sudo checks and static
network, password login, DNS/HTTPS, reboot/key persistence and graceful
shutdown checks on clones 3122/3123. The builder fixes upstream doas username
settings and cloud-init helper path, and generates machine identity from the
clone SMBIOS UUID. The machine-ID regression test executes the actual boot
script and checks stable, distinct 32-character identities. Commit 3648c5a
contains the verifier manifest and builder regression. Rejected prototypes
3012/3016 and their disposable clones remain to be removed after review.

OPNsense 26.7 Nano source checksum/signature passed, using matching public
keys from the official release README and GitHub changelog. Build VM 3013
booted, but auto-assigned LAN=vtnet0 and WAN=vtnet1, contrary to the expected
shared-facing WAN NIC. The shared-facing NIC was immediately moved onto
isolated bridge crvfyop with link_down=1. Both NICs are now isolated. The
builder now starts both NICs isolated; guest interface assignment must be
verified before connecting WAN to shared access. OPNsense is not a template
or approved catalog entry yet. Guest seed customization, SSH/guest-agent
setup and clone tests are outstanding.

## Required custom jump-box acceptance

Custom jump boxes remain a required deliverable, separate from the base OS
template catalog. The current implementation still fixes legacy jump boxes to
RDP and exposes one protocol per scenario guest; it does not satisfy this work.

- Project managers can manage project-scoped jump-box templates derived from
  approved VM and container bases, name instances, and mark a scenario VM or
  container as a jump box without replacing the existing guest.
- Each box can expose selected SSH, RDP and VNC connections. Assigning a box to
  a Guacamole user grants its selected services, with team/subnet/IP shown.
- Instances can use the selected team's network or an authorized custom
  network. Team 0 maps to staging network 254; inferred team fields stay hidden.
- Connection credentials default to user/toor. Managers can provide protected
  custom credentials and choose whether users see a credential popup. Secrets
  must stay out of Git, job output, release manifests and ordinary API responses.
- User defaults and batch creation can provision multiple jump boxes per user
  from the chosen templates, with project-scoped permissions and quotas.
- Changes use audited jobs, explicit staging/release operations and scoped
  configuration. Editing staging must preserve deployed teams and unrelated
  Proxmox services.

Acceptance requires real VM and container instances, multiple Guacamole
services, custom-network placement, credential disclosure settings and
authorization checks. Do not report custom jump boxes complete based only on
new OS templates or existing fixed-RDP jump boxes.

## VM/container jump-box checkpoint (2026-10-01 UTC)

Named jump-box roles and SSH/RDP/VNC service selection are implemented for
existing VMs and containers, without replacing guests. Team pages and assigned
user pages include these boxes; each box assignment grants all selected
services. Custom connection names include Team 0 or the owning team so a
captured release can reuse names across staging and deployed teams. Release
manifest/declaration helpers retain role/name/service metadata. Released guest
keys are accepted by the typed guest controls while project lookup remains
mandatory. See docs/custom-jump-boxes.md for scope and acceptance evidence.

All 150 controller unit tests pass, router Ansible syntax checks pass, and
JavaScript syntax/grouping checks pass. Live Project 240 jobs promoted VM 130
and CT 129 on custom staging subnet 100. A separate native Guacamole login
received six connections; native HTTP tunnels produced interactive display
frames for VM RDP and VM/container SSH. Removing CT VNC revoked its grant and
exact endpoint firewall permission while retaining VM VNC permission. Guest
configuration hashes/PIDs and unselected team guests remained unchanged.
The temporary native login was deleted through the audited API workflow.
Custom credential/disclosure settings and reusable jump-box template/default
provisioning remain required; this checkpoint does not finish custom jump boxes.

Release 1.2.0 cleanup is physically complete: test IDs 144–148 are absent and
source/team guests and approved Linux VM bases remain present. OPNsense build
VM 3013 now has verified native WAN/LAN assignment, factory DHCP disabled,
MTU 1450, key-only root SSH, sudo and a working guest agent after reboot.
It is still a build guest, not an approved template: clone seed customization,
identity separation and lifecycle acceptance remain outstanding.

During the earlier OPNsense build, factory LAN DHCP briefly reached shared
access before the guest's unexpected interface assignment was detected. The
builder now starts both NICs isolated. Shared gateway 102's saved LAN address
was correct, but an unmanaged LAN DHCP client had replaced its runtime IP.
Only that client's supervisor was stopped; native LAN reconfiguration restored
the saved address. WAN DHCP stayed running; the shared firewall was not
rebooted and its saved firewall rules/configuration were not changed. Direct
controller DNS and registry HTTPS recovered and release cleanup succeeded.
Keep isolation/interface/DHCP guards in the reproducible OPNsense builder.

Selected-team rollback acceptance, repeatable VM boot-order normalization,
Alpine workflow/catalog acceptance, prototype cleanup and remaining lifecycle
status/resource reporting still require work before the goal is complete.
