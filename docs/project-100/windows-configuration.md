# Windows guest configuration contract

`crange configure PROJECT windows --yes` initializes applied Windows scenario
VMs before it builds the private WinRM inventory. The fixed worker operation
accepts only a declared `scenario_boxes` key whose applied placement matches an
approved Windows template and a live, non-template Proxmox VM with the expected
project/name/VNet tags. It rejects templates, undeclared guests, and the sealed
source and verification probes. MAC, static address, gateway, DNS, account, and
WinRM source are derived by trusted worker code. It does not accept a script,
path, or address from the GUI.

The guest operation waits for Windows Setup to report `IMAGE_STATE_COMPLETE`
and completed OOBE fields. It does not fake Setup registry state. It then sets a
unique random password on the local RID-500 administrator, configures the
declared adapter's static network, and enables WinRM only from controller
`10.60.0.20`. Network settings are established before the WinRM readiness
baseline connects to the declared guest address.

The worker stores the resulting credential bindings in separate private stores.
A mode-0600 pending journal outside Git holds the generated RID-500 password,
the separate local RDP account password, and the exact QGA PID while
initialization runs; it records raw terminal output before parsing it. The
account `user` is added to the built-in Remote Desktop Users group. Neither
password is printed, added to Terraform state, or written to generated
inventory. Once stored, repeated configure runs repair access/network settings
without rotating either password. A changed guest or image identity requires a
fresh initialization. RDP password disclosure in launch notices defaults to
false.

The Windows WinRM baseline uses the private RID-500 binding. Guacamole manifests
may carry a typed, nonsecret RDP reference bound to project, box key, applied
guest ID, and approved image ID. The connector resolves it at launch from the
separate private RDP store. A Windows RDP connection without a matching binding
fails closed rather than using the legacy shared `toor` default. Explicit
project-managed custom credential references retain precedence; their username
must already exist in the guest. The worker-created local Administrator
credential remains private to WinRM and is never used for Guacamole.

This initializer supports workgroup guests. A business scenario that needs
Active Directory promotion requires a separate reviewed domain-join workflow;
the local-account initializer must not run after promotion.

Initialization is a live Proxmox operation and must only be run after a real
declared Windows clone is applied and its approved image has been published.
Offline tests exercise ownership rejection, private journaling, QGA request
construction, and password generation; they do not claim live Windows readiness.

## Captured domain scenarios

The deployment initializer now selects a separate contract for approved
project-scoped Windows snapshots. It verifies current guest ownership, applied
logical image and replica identity, preserved computer/domain identity and a
new VM generation ID. It derives the declared adapter, target static IP, gateway
and domain-controller DNS from the release and its LAN cohort. It preserves
accounts, hostname, domain membership and existing service configuration.

Source management/RDP bindings are frozen before future captures in a private
mode-0600 store outside Git. Older captures can recover their existing source
binding from verified release lineage; the management password must authenticate
in the clone before its new private binding is accepted. QGA input carries the
secret on stdin, and job output withholds guest errors. Submitted commands have
a durable PID journal; a timeout resumes observation rather than resubmitting.

The fixed recorded MHL staging domain workflow also uses this preservation path
when configured again. Generic workgroup bases retain their existing unique
account initialization. Snapshot domain controllers use domain-qualified
management logins, and captured controller RDP usernames infer the DNS domain.
No user is asked to select a realm, guest MAC, physical replica or controller IP.

These changes are deployed and covered by offline ownership, journaling,
credential-preservation and replica checks. Fresh release clone configuration
and authenticated desktop acceptance remain pending; they are not proven by
unit checks alone.
