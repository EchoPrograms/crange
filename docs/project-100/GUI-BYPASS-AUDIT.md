# Project 100 GUI bypass audit

## Current checkpoint — October 19

Actual browser actions published service-update release 0.1.3, preserving the
six immutable parent images. Its CI artifact credential authenticates in staging;
fresh 0.1.3 testing and explicit company rollout remain pending. The cache restore
and isolated Windows probe recovery finished successfully; no repeated restore
was needed. Hardware diagnostics identified Proxmox6's failed RAID-cache capacitor.

The exact dashboard apply payload for reviewed workstation plan
5613c70c92cea61d9bf5675253ed618d failed at the subprocess timeout after creating
seven Windows guests on Proxmox6. Both timeout layers are fixed. No guest
configuration followed, and workstations/desktops are not accepted. All ten
participant native logins passed, without self-password change permission.

Unexpected Proxmox1 loss currently blocks reservation reconciliation and a fresh
plan. The controller's verified API failover reads through Proxmox4. Operator SSH
was used for infrastructure diagnosis, protected code deployment, and a temporary
local HTTP transport; lab operations continue through authenticated dashboard API
schemas. No offline ownership checks, node fencing, or quorum gates were bypassed.
The owner has been asked for console observations. Deep assessment, workstation
desktops, isolation and reset/rollback remain unaccepted.

## Historical checkpoint — 2026-10-16

The earlier findings below are historical. Current evidence is tracked in
[STATUS.md](STATUS.md) and [FEATURES.md](FEATURES.md): staging Linux/AD/file
services are configured, release 0.1.1 is published, all six original servers
are running, and a fresh isolated release-test cohort is declared through the
authenticated dashboard API. Recipe preservation/replay is automatic within
capture, fresh-test preparation and release rollout; no extra PM maintenance
button or arbitrary automation is exposed. Fresh complete-release tests,
participant deployment/grants and rollback remain outstanding.

## Historical audit and subsequent acceptance

This audit compares Project 100 lab work already recorded in the progress and
acceptance documents with the actual dashboard buttons and fixed job worker
operations. A CLI command or passing source test is not evidence that a
project manager can perform that action through HTTP. “Acceptance pending” means
the GUI path still needs an authenticated browser request and resulting
job/runtime evidence.

| Action | Actually done (method/evidence) | Existing GUI backend | Missing feature | Acceptance HTTP/button request and evidence pending |
|---|---|---|---|---|
| Windows unattended base preparation, locale adjustment, sealing and proof capture | VM 115 was prepared through fixed administrator/root CLI and Proxmox-side work. Candidate 8912 and independent probes 8913/8914 have sealed/probe evidence in `windows-progress.md`; this does not exercise dashboard preparation. | Templates now has typed unattended Windows Server 2025 build creation plus administrator-only `template-windows-base-prepare` and `template-windows-base-seal`, selected only by owned ledger build ID. Worker generates a per-build private seed ISO/password, verifies live owner/config/QGA/network, journals one fixed QGA PID, deletes the seed after setup verification, and requires successful Sysprep plus VM shutdown before allowing publish. Post-OOBE initialization authenticates unique Administrator/RDP credentials before removing only marked CRANGE/Panther answerfiles and auto-logon secrets. | Fresh unattended base runtime, published image, and protected clone identity proof have not yet been exercised. Base receipts are not clone proof; P100 clone capture remains a separate action. Older builds without the exact private binding stay ineligible. | Authenticated HTTP acceptance against sealed candidate 115 returned HTTP 202 and terminal failed job `fc07074b369b408abba838bb31f83a3d` with the actionable sealed/identity mismatch before any QGA guest call. The response was secret-free. Runtime preparation/sealing and clone proof remain pending a fresh eligible owned build. Sealed P100 publication action remains distinct: `POST /api/jobs` with `{project:0, operation:"template-publish-sealed-windows-p100", options:{}}`. |
| Exact-VNet verification and owned probe recovery | Operators prepared disposable probe plans, requested root review, diagnosed 403s and used fixed CLI/ACL-repair and reconciliation steps. First probe failed before checks and was cleaned; later probe preparation likewise stopped at 403. Authenticated GUI job `b78fd7f848f845fe98fe6543abfb9f04` subsequently passed all six checks and produced a source-bound schema-2 receipt for Project 100 / proxmox2 / p100t254, with independent cleanup confirmation. | `network-verify` and `network-reconcile` are fixed job worker operations in `crange_jobs.py`; Jobs UI has a reconcile button for admin users when a verify job reaches `reconcile-required`. | Verification and owned recovery now have authenticated runtime evidence. Publishing the validated exact-node/VNet receipt into placement admission is still a missing integration; the receipt does not certify other nodes or team networks. | Project manager/admin browser request from the network page and the resulting HTTP job(s), including a failed-probe recovery. Verify accepted VNet receipt and exact cleanup evidence; RDP/3389 remains a separate required check. Current payload schemas: verify `{node:"proxmox1".."proxmox5", network:"p100tNNN"}`; reconcile `{source_job_id:"<32 lowercase hex job id>"}`. Confirm route/authorization behavior and actual button evidence. |
| MHL Linux business application and AD service configuration | Roles/playbooks exist; the four Team 0 containers and Windows servers are not provisioned yet. | The Team detail page now has fixed `mhl-business-configure` and `mhl-windows-configure` actions scoped to its selected network. Linux validates all four canonical applied CT identities and uses private per-network secrets. Windows binds only the declared canonical DC/file-server roles to an independently keyed forest vault; the GUI accepts no host, playbook, or secret fields. | Runtime service and AD health checks remain pending until declared guests are deployed and Windows guests have private WinRM bindings. Linux profile metadata is retained on release without secret values. | Linux request: `{project:100, operation:"mhl-business-configure", options:{network_id:254}}`; Windows request: `{project:100, operation:"mhl-windows-configure", options:{network_id:254}}`. For either, `network_id:1..5` requires that team to be selected explicitly after release. Source tests cover schemas, applied identity, strict OS/role binding, and private credential handling. Authenticated HTTP prerequisite acceptance is pending. |
| Windows guest initialization and private RDP credential binding | Trusted initializer and typed credential reference are implemented; no applied Project 100 Windows clone has been initialized. | Project overview now exposes `Configure Windows guests`, using the fixed existing `configure PROJECT windows --yes` path. It selects declared/applied Windows guests only and keeps WinRM/RDP bindings in worker-private stores. | Runtime guest acceptance remains blocked on applied Windows guests. | Authenticated `configure` request with `{target:"windows"}` and an applied declared clone is pending; verify guest readiness, stored binding resolution, and no credentials in response/logs. |
| Automatic per-user jumpbox declarations, user fixtures and Guacamole grants | Ten users and their scoped Linux jumpbox assignments were created through authenticated dashboard/API jobs; Guacamole accounts were confirmed. Disposable user lifecycle operations were exercised through authenticated API and fixed jobs, then deleted. | Existing `guac-enroll`, `guac-users`, account/user operations and jumpbox/scenario declaration controls. Project 100 user fixture and assignment HTTP workflows are recorded as done. | Deployment of the actual boxes and resulting per-user connection grants remain pending guest/network readiness. Do not treat account setup as proof of usable jumpbox access. | After deployment, use the GUI deploy/refresh path, then verify each account sees only its assigned Linux/Windows boxes and can connect. Existing user account evidence includes successful job IDs in `platform-progress.md`; no passwords or private JSON payloads are repeated here. |
| Scenario declarations, plans, deploy, health and releases | Linux boxes and four staging containers are declared through fixed CLI; no Project 100 guest plan/apply was produced. Releases have not been captured/tested/deployed. | Scenario page supports declarations, stage/review and deploy controls. Worker schemas include scenario/container add/edit/remove, machine state, health, release capture/test/deploy/upgrade/rollback. | Runtime acceptance still pending, but no immediate operator-CLI-only bypass is established for these declared functions. Current GUI controls must be exercised end to end after network and image gates clear. | HTTP/browser evidence for staging edits → stage → inspect retained plan → deploy → health; then capture immutable release, fresh-clone test, selected-team rollout and rollback. Payload schemas are already fixed in `crange_jobs.py`: declaration fields include `name, team, network, ip, template, node, cores, memory, health, guac_protocols`; release capture has `version, boot_order`; deploy/test have `version` and selected `teams` as applicable. No job/runtime evidence yet. |
| Operator infrastructure break/fix: Proxmox ACL repair, Tailscale/host management, controller/code deployment | Root/operator SSH and cluster/API operations repaired or investigated host access, worker ACLs and deployed code. These are platform administration actions, separate from project-manager lab lifecycle. | No project-manager button is expected. | None for the project-manager GUI scope. Keep privileged infrastructure break/fix in operator workflows. | Not applicable to PM acceptance. Record operator change evidence separately; do not expose host/root shell through the GUI. |

## Highest unowned gaps

1. **Authenticated acceptance of MHL service configuration.** Both fixed
   service actions now exist, but applied guests and an authenticated HTTP
   prerequisite run are still needed to prove the worker response and later
   health checks.
2. **Windows guest initialization and private RDP bindings.** Windows
   participant jumpboxes cannot be initialized or connected through the
   intended private credential flow until the trusted hook is deployed and an
   HTTP action targets an owned applied guest. This directly blocks actual
   Windows access even after image publication.
3. **Windows base preparation and proof capture UI.** The sealed publish button
   exists, but the prerequisite unattended locale/seal/capture work is still
   an operator CLI sequence. Until a fixed GUI backend wraps those steps, a
   project manager cannot move a prepared candidate through publication under
   the reviewed workflow.

The network verification/recovery wrapper is actively being built, and the
Windows publication wrapper is assigned. Those tracks should use this audit to
avoid duplicating those actions. This document does not establish GUI acceptance
for any feature solely from the existence of a CLI command, backend schema, or
passing source tests.

## Authenticated GUI acceptance recorded on 2026-10-08

- Sealed Windows publication: the exact Templates action
  `POST /api/jobs` with project `0`, operation
  `template-publish-sealed-windows-p100`, and empty options succeeded as job
  `3a07826c1c5a47b4b5476ef031656911`. An authenticated Templates read confirmed
  image `mhl-windows-server-2025-base-p100-v1` published with the verified archive
  checksum. This establishes publication only; local linked-cache preparation,
  project template approval, and unattended base preparation remain separate.
- Network verification: authenticated project `100`, operation `network-verify`,
  options `{node:"proxmox1", network:"p100t254"}` returned HTTP 202 for job
  `91455a2d13de4643b4976fa4c140eb46`. It failed during bundle preparation before
  lifecycle execution. This is a reproduced GUI failure, not network acceptance.
  Protected subprocess diagnostics and the generated Terraform inputs are being
  corrected before a new button request.
- MHL service prerequisites: authenticated requests returned HTTP 202 and
  terminal, actionable failures for Linux services (`mhl-business-configure`,
  job `8bbc798f65764b45a07f38e47992c0cb`: the canonical four staging containers
  are not deployed) and Windows AD (`mhl-windows-configure`, job
  `86777a944df6479ea9a912e06c07db9c`: no applied Team 0 Windows guests). No
  guest configuration ran. The generic `configure` action with
  `{target:"windows"}` returned HTTP 202 and failed before guest action as job
  `8cac042ed4864edeae186d8868370386`, reporting that no declared Windows guests
  are available and the reviewed scenario release must be deployed first.
  These checks establish an authenticated, actionable preflight only; they do
  not establish service health or guest acceptance.

- Network recovery: verification job `943476557fe346d8a73a65cefccb2639`
  reached fresh capacity admission and was rejected for insufficient CPU on
  Proxmox1 before apply. The authenticated `network-reconcile` action, job
  `66408e99f7cd4329be1ce52bf14c090d`, succeeded and recorded all protected
  no-admission checks: no reservation, no VMID, empty disposable state, no
  temporary rules, and unchanged inputs. The original job became terminal
  `failed_cleaned`. This validates recovery, not network acceptance.

Operator-only steps must be replaced by fixed, authorized actions with typed
inputs; merely exposing a CLI command or importing old proof is insufficient
to claim that a fresh lab can be built through the dashboard.

## Latest runtime evidence and unfinished handoff

- GUI network verification job `b78fd7f848f845fe98fe6543abfb9f04` succeeded.
  Its schema-2 receipt SHA-256 is
  `9dd675b139c3f625f3b5588b9e5a6e0d0727f1f896e33394b73e8c4cffae2d71`.
  Scope is exclusively Project 100, proxmox2, p100t254. All six checks passed,
  including actual Guacamole VM 110 source address 10.60.0.10 to the owned probe
  TCP 22. The receipt binds seven controller source hashes, the host relay hash,
  provider lock, and dirty-source metadata. Cleanup confirmed guest absence,
  empty disposable state, absent temporary rules, and a released reservation.
- Capacity-policy GUI job `298bd3c44b68432cbc8c8e8ce35b76bf` succeeded, setting
  CPU ratio 5, RAM ratio 2, and linked-local virtual disk ratio 2. It preserved
  node/storage approval and physical reserves. P1 CPU headroom increased 1 to 4;
  physical disk and RAM headroom did not change.
- Windows cache GUI job `d6598a9307ac4e639620a92207d58d84` failed readiness.
  Its inner journal `cache-15f3498e404342fbb096bd1f9012b4b2` remains verifying,
  with owned stopped template 122 and probe 123 on proxmox1 and a held ledger
  reservation. Do not retry allocation: implement fixed GUI recovery and
  persist the actual readiness error/PID before classification.
- Windows installer GUI job `9f1fd4bc4ad448219767819470a95b5b` admitted a
  40-GiB disk on proxmox5 but failed multipart forwarding before VM creation.
  Its reservation was released; the exact seed volume was absent. A benign
  A/B diagnostic established that direct selected-node upload works while the
  proxmox1-forwarded upload loses its temporary-file handoff. The benign volume
  was removed and independently confirmed absent. Direct upload and durable
  seed recovery changes remain incomplete until their safe cleanup transport
  is integrated and deployed. No broad storage-admin privilege was granted.
- Dashboard account project IDs now parse strictly; malformed values cannot
  serialize as null. Mixed-case usernames canonicalize consistently at create
  and login. Password validation explains the dashboard 12-character minimum.
  Visible deletion protects self/last administrator, and duplicate create
  cannot overwrite an existing account. Authenticated create/login/delete and
  rejection checks passed without logging credentials.
- The Guacamole extension deployed as SHA-256
  `661ad960d1113e2b7f7a5946d402e71fc92eecee2e4f08981cc237358581a1c3`
  removes both prominent native-connection loading placeholders, including
  “Verifying native connections…”. Only a fixed-width title spinner remains;
  verified rows persist during an in-flight refresh. Project selection retains
  a valid saved value or defaults to All projects. Actual served JS/CSS and
  extension hashes were checked after deployment.

The full lab remains incomplete: business guests, Windows access, release
acceptance and five-team rollout are not established by these checks. Worker
turns ended at an account usage limit; inspect the recorded live handles and
worktree before resuming. Never infer failure from the worker agent ending.

## Solo implementation and runtime checks (2026-10-09)

The user stopped the agents and requested solo execution. No agents were
restarted for the work below.

- Deployment comparison proved the controller still ran the old seed uploader
  (`e194cfa3…`) without direct-node upload or a durable upload journal. The
  reviewed helper (`f4131aa2…`) was deployed before retrying.
- The visible seed-reconcile button referenced an unsupported `template-cancel`
  job. The fixed admin-only API mapping is now implemented. A pre-journal
  recovery branch checks exact reservation/build identity, VM absence, ISO
  absence on all enrolled nodes, and no active upload tasks before release.
  GUI job `622021ad14ef466f9d7f72e2865eb79f` succeeded, releasing the stranded
  allocation from failed create job `72429a2fcb224c938ea9860f7eb3687b`.
- GUI create job `1f09968a22ee4050ad72b8f99ce62f6a` succeeded, uploading its
  private seed directly and creating P5 VM 123, build
  `fce642d72e594756ae45a84158042266`. OS installation initially stalled at the
  official ISO's DVD-key prompt; console inspection proved this, rather than
  inferring it from a guest-agent timeout.
- A fixed admin-only Start Windows installer action now validates the owned
  fresh build, original power session, zero disk writes, and idle firmware.
  It persists reset task handles and bounded key progress. A dedicated
  VM.Console-only role was granted to the existing worker user and token on
  `/pool/crange-iac`, after confirming that pool's members were range-owned.
  No permissions outside that managed pool were changed.
- Boot job `61332501341145aa849d808f8f9ea39c` succeeded using the selected node's
  direct endpoint. Subsequent live status showed about 4 GB of disk reads and
  over 6 GB of writes: installation actually began. Preparation/sealing remain
  unverified. The GUI now disables preparation without a Windows guest agent
  and refuses boot resets once disk writes begin. Identity comparison excludes
  the detachable seed and PVE configuration digest while tracking hardware.
- The Team 1 test previously failed solely because it assumed a different-node
  gateway. The new path validates the actual cross-node project transit when
  the gateway is local, checks the live upstream route and both router configs
  before/after, and records the segment tested. It does not claim that the local
  LAN itself crossed hosts. GUI job `ff1fb38103f9414ca2e65ce07b426fb9` passed
  all six checks, cleaned its owned probe, and automatically published only
  P100/proxmox1/p100t001. Receipt SHA:
  `9a9dd2f830728d4f20afe2b2c65932f28628391bd4e570001b78a557b95a9747`.

Remaining work includes the other team-network prerequisites, reviewed scenario
application, actual Linux/AD service health, participant boxes/access, and the
immutable release/test/rollout workflow. None is implied complete by these
installer and network checks.

## Continued solo acceptance

- Team 2 verification GUI job `5cc18da09d284078b8206def4d733fdc` succeeded
  and published only proxmox1/p100t002 after cleanup. Receipt SHA:
  `09efa75bb9a0a1e51872bf235db59e3bba2ef6d1d0b1eb84abb90a863a47ee7b`.
- Remaining same-button requests: Team 3 `78e0df7cee4547ddbb1686b36b470c02`,
  Team 4 `d1057fa13721473f92304c1728cef446`, and Team 5
  `bc077e5b0a5649af91f7ed5769076323`. Observe their existing handles; do not
  infer completion or submit duplicates.
- Windows guest-agent readiness became available. Preparation GUI job
  `0b17fcf99ecb4c1a9a6a775d335e6846` was explicitly denied at guest-exec HTTP
  403. The installed PVE API definition requires VM.GuestAgent.Unrestricted;
  CrangeDeploy supplied only GuestAgent.Audit. A dedicated single-privilege
  CrangeManagedGuestAgent role now grants the worker user/token that capability
  only on the reviewed managed pool /pool/crange-iac. CrangeDeploy was not
  edited and no host/global permission was granted. This is a deployment
  prerequisite for reviewed Windows initialization and guest-user workflows.
- The GUI reported preparation not-started after the denial. Same-build retry
  job `930ade0c836e4add832f1b5ff7f58ed1` was submitted after the scoped repair.
  Its execution and resulting preparation/cleanup remain to be verified.

## Sealing and full P1 team-network coverage

- Windows sealing GUI job `8553d80083184c10b4178a7aa780c73b` succeeded for
  build fce642d72e594756ae45a84158042266 / P5 VM123. Its output records
  successful Sysprep and observed shutdown. Seed cleanup now accepts PVE's
  normal `size=376K` media attribute while matching the exact volume identity.
  This is sealing acceptance, not clone identity or logical-image publication.
- Team 3 job `78e0df7cee4547ddbb1686b36b470c02` and Team 5 job
  `bc077e5b0a5649af91f7ed5769076323` passed all six checks and published exact
  P1 network scopes after owned cleanup.
- Team 4's first job lost the error while polling an accepted DHCP process.
  BoundedPVE now retains safe error categories, and fixed-check logs retain the
  accepted PID. Only read-only GET polling retries once within the existing
  deadline; guest-exec POST is never replayed. Targeted suite: 67 tests, one
  preexisting skip. GUI retry `66c53fb2fa934c1f8eb72ac01ca96a10` succeeded and
  published proxmox1/p100t004. Receipt SHA:
  a85101e78041d696b894f28dfef7229561cca910e1753f1b7889ec16fe16505e.
- All five ordinary team VNets plus staging now have P1 receipts. Other-node
  certification is not implied. The next Stage Changes job must still prove
  actual resource/image feasibility and a reviewed plan.
- Generic template-publish currently converts the physical VM and registers a
  raw template alias, without exporting/publishing a verified immutable logical
  image. That feature gap remains open; do not claim the new builder is ready
  for automatic distribution. The prior immutable Windows image remains ready.

## October 14 current acceptance update

The earlier rows are historical, not the current deployment status.

- Six staging business systems are deployed and configured. Exact authenticated
  job and health evidence is recorded in STATUS.md and the service progress files.
- Complete immutable release 0.1.0 capture succeeded as dashboard job
  `06d1a19051ab4b96858d09b2a64764ff`; this proves per-image capture/clone
  acceptance, not a fresh full-release service test or participant rollout.
- Test preparation, custom-LAN foundation recovery and exact P1/P5 network
  acceptance ran through authenticated fixed dashboard jobs; see STATUS.md.
- Cluster storage creation and container-cache acceptance are now administrator
  GUI actions. Storage apply `2782f84fbebe4020bf5a3c5f9038c505` and container
  acceptance `d8482e3fdb6c4c7ab7a340391484ab13` succeeded. Acceptance proves
  physical linked backing, independent identity/readiness and owned cleanup.
- Stage Changes `6720b03cc29f46bb982e62d24edddee6` is preparing the remaining
  fresh-test caches. Test guests and participant rollout remain pending.
- No guest, storage or network mutation in these steps was performed by an
  arbitrary controller shell. Read-only diagnostics and reviewed source/service
  deployments remained operator actions. Fresh browser rendering and all final
  feature exercises remain separate acceptance requirements.

### Fresh cohort deployed, service preflight pending

Stage Changes `935165c1362f4f928790c31f3daca3c9` succeeded after correcting
Terraform validation for semantic-version VM/CT keys. Reviewed plan
`b8cdc6dfba690dc2fbeeebe6d31a46f3` had six creates and no update/deletion/
replacement. Stage Deploy `e7c646b8aacd45db9b153fc4eb234ee3` succeeded through
infrastructure, scoped configuration, authenticated Windows management and Guac
refresh. Two fresh Windows VMs and four containers are deployed.

Release test initially used node `auto`; the fixed live-cluster resolver passed
on retry `7955ae0db3e047bb96ce423b3f49e88d`. That retry reached Windows
service replay and stopped at a preflight excluding test LAN100, before guest
writes. Fixing this hashed recipe requires an updated staging recipe and a new
release; it does not justify changing the immutable 0.1.0 manifest.


### October 16 maintenance capabilities added before use

- Interrupted completed archive restores now have **Templates → Publication &
  recovery tools → Verify restored cache**. The live button submitted
  `609d1dcbced24ccbb72ec633535ef1a3` and completed publication/probe cleanup.
- Archive headroom now has **Cluster → Maintenance & capacity settings →
  Library & storage → Grow archive by 64 GB**. The live button submitted
  `7215e99442154084af7cc0ec95985ba4`, growing the exact data volume online.
- These were implemented as fixed audited worker jobs before runtime use.
  SSH was used to deploy reviewed capability code and collect read-only
  diagnostic evidence; no ad hoc runtime restore or resize bypassed the jobs.
- Live use also exposed refresh destroying an open action menu. Menu state now
  survives same-context renders while explicit dismissal remains closed.

### October 18 — service recipe update and Windows cache recovery

Added a fixed release-recipe job and actual Publish service update form rather
than rewriting a release manifest manually. Browser job a892893ef6d74c9ea2bd149c1cf794b3
published 0.1.3; its independent manifest/retention evidence is recorded.
Staging service configuration used the existing browser action. Windows cache
recovery submitted the exact admin template-cache-restore-resume API payload
with source_job_id cache-9d9d22dc04d24e349d31f75244fad7ad, job
c6dfdbc9e340494c93d0fc8481c6283b. Root inspection was read-only; machine changes
are performed only by the reviewed disposable-probe workflow.
