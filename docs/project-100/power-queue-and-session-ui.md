# Power queue and session controls — October 20, 2026

The controller's normal automation queue remains serialized. It no longer
serializes every interactive power request behind that queue:

| Execution path | Concurrency | Scope |
| --- | --- | --- |
| Normal automation | 1 | Reviewed infrastructure, configuration, capture and administration |
| Routine power | 4 | Start, shutdown and restart; Control and Access |
| Emergency Force Stop | 1 | Independent of a waiting graceful shutdown |
| Privileged network verification | Separate worker | Existing isolated verification/reconciliation |

The live normal worker has six threads: one normal thread, four routine power
threads and one emergency thread. Routine controls on different projects or
different idle guests can run concurrently. Controls on the same guest stay
ordered. Project-wide configuration/lifecycle operations block conflicting
controls; reviewed guest-only applies and selected-machine captures identify
their guest scope so unrelated controls can proceed. Router/network changes
retain project scope. Force Stop retains its emergency override and supersedes pending Start, Restart
and Shutdown jobs for the same guest.

CLI project locks coordinate normal automation with router HA metadata adoption.
Interactive power controls do not hold that lock while waiting for a guest OS.
Short configuration-write locks merge the latest observed power flags so
concurrent controls and unrelated configuration edits do not overwrite one
another. Authorization is checked when the job is claimed, and guest identity
and network ownership are checked before Proxmox operations. Jobs remain audited
and visible; the UI does not receive arbitrary execution capability.

Control's jump-box Start/Stop previously only changed desired configuration.
For a deployed jump box those actions now operate the guest directly; an
undeployed declaration retains the desired-state behavior.

## Access toolbar and status indicators

Clicking the drag grip collapses the toolbar into an 18px-wide edge handle with
an inward arrow (24px on coarse pointers). Top/bottom edges use the horizontal
equivalent. Dragging the handle selects the nearest screen edge; clicking
restores the floating controls to their saved position. Keyboard arrows and
Home remain available. Pointer cancellation, lost capture, blur, and a second
pointer cannot leave a stuck drag. Hover help disappears immediately on drag
start and cannot appear mid-drag.

Control and Access show neutral Running text; the dot retains green and a
subtle 2.4-second opacity pulse. Reduced-motion preferences disable animation.
Non-running indicators do not receive that green pulse.

## Verification

- Full Python suite: 1,202 tests run, one skipped, no failures at the deployment
  checkpoint. This includes the separate router HA implementation.
- Concurrent worker regression: four different guest controls enter together;
  a second control for the same guest remains queued. An unrelated project's
  power action proceeds while another project's configuration is busy.
- Configuration regression: concurrent observed power changes survive an
  unrelated resource edit and subsequent power changes to another guest.
- Guacamole workspace, hover help, UI, and settings JavaScript checks pass.
- Chromium desktop and narrow viewport fixture uses the actual extension
  directive and styles. It verified click-collapse, edge drag, click-expand,
  viewport bounds, neutral text/green dots and reduced-motion behavior. The
  final slim handle measured 18×56px and docked at x=1422 in a 1440px viewport.
  Physical touch hardware and a screen reader were not exercised.
- Guacamole restarted with user authorization. The service returned HTTP 200;
  the installed extension hash is
  `65c537fa41744bdb57fcccc56511f4e57671889eeda92da670757a9c3f5ddd8c`.
  Public combined JavaScript/CSS include the new toolbar and status rules.
- Controller web, both workers and router HA timer are active. Source hashes
  were checked against the local deployment payload.

The ordinary queue is still a single lane: this change deliberately adds a
bounded interactive power path, rather than permitting simultaneous Terraform
or shared-network mutations without resource-level scheduling.

The shared authorization database directory now permits both service accounts
to create SQLite journals (setgid group access, 2770). Parent directories keep
traverse-only group access. A real worker transaction was inserted and rolled
back successfully; this fixes the final audit write during permanent deletion.
