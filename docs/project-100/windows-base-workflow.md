# Windows installer base preparation

The Templates ledger has fixed administrator jobs to prepare and seal an owned
Windows installer build. Both jobs accept only the 32-character build ID. The
worker verifies the ledger and live Proxmox name, owner tags, node, VNet,
QEMU-agent setting, VM/template state, and a per-build private answerfile
binding before it sends one of the reviewed PowerShell scripts through QEMU
Guest Agent. The dashboard cannot supply a VMID, shell command, script, path,
or credential.

Preparation and sealing use a private, mode-0600 worker journal bound to the
build and live configuration identity. The journal keeps the QEMU-agent PID
and protected terminal output. If a job times out, the next request polls that
same PID; it never sends Sysprep again. The seal receipt requires a successful
fixed-script result and observed shutdown. It is not clone identity proof and
does not publish a template.

The Templates form has a typed unattended-install option for the fixed
Windows Server 2025 Evaluation ISO. It requires the approved VirtIO ISO so the
fixed setup hook can install the signed QEMU Guest Agent and serial driver.
The worker generates a new answerfile ISO for each build, with a random local
Administrator password saved in a worker-owned mode-0600 credential file and
bound to the build ID, VMID, and answerfile checksum. `xorriso` is a controller
runtime dependency. The seed uses only fixed repository template files and
fixed ISO-generation arguments; no paths or script contents come from the UI.
After the fixed preparation script verifies the installed guest, the worker
detaches and deletes that exact per-build ISO. The installed private clone
answerfile remains on the owned VM for Sysprep. The seal action runs Sysprep
as the local Administrator through a one-shot task, then requires successful
QGA completion and observed VM shutdown before recording a seal receipt.
Publishing an unattended Windows build is blocked until that receipt exists
and the generated seed ISO is detached. Older builds without this exact
per-build answerfile and private credential binding remain ineligible.

After each clone completes Windows OOBE, the fixed guest initializer applies
and authenticates the worker-held unique Administrator and RDP credentials.
Only after both credentials authenticate does it remove the answerfile at the
fixed CRANGE and Windows Panther paths that contain the owned base markers,
then disable/remove Winlogon auto-logon secrets. A failed initializer retains
its private retry journal and leaves credential registration incomplete; it
does not claim the clone ready.

Do not use this flow on the frozen P100 candidate or template IDs 115/8912, or
on unresolved builder VM 145. The immutable-tag, live-template, and exact
ownership checks reject those records. Clone OOBE, unique SID/MachineGuid proof,
and publication remain separate steps.
