# Guacamole native connection reconciliation

The dashboard keeps native Guacamole connections separate from CRANGE project
boxes. Native rows have no synthetic project ownership and never receive power
or snapshot controls. The authenticated Guacamole bridge resolves connection
IDs, hostnames, and current READ access on the server; it matches hostnames to
the current non-template Proxmox inventory and returns actual power state only
for a unique live guest match. Unmatched native connections are omitted. The browser filters the authenticated connection
tree against the latest verified inventory before creating rows. Data source is
part of the lookup and link identity, so an ID collision in another Guacamole
source cannot borrow a matching IP or URL. If verification fails, previously
verified rows remain with unknown power and an actionable error; unknown or
deleted rows are hidden.

On 2026-10-08, the live Proxmox cluster inventory contained 45 guests and 21
configured or guest-agent IPv4 addresses. Guacamole IDs 1, 2, 3, and 5–10 had
no match to a current non-template guest address. Their connection names,
protocols, hostnames, parameters, and per-user permission records were saved
before removal. Those nine records were removed from Guacamole. ID 4,
`crang-controller`, was retained; it maps to running controller container 114
at `10.60.0.20`. The live Guacamole inventory now contains that one native
connection. The controller appears as a live unmanaged connection and has no
CRANGE power controls.

| ID | Removed connection | Target |
| --- | --- | --- |
| 1 | `parrot-jump-01-rdp` | `10.61.250.20` |
| 2 | `parrot-jump-01-ssh` | `10.61.250.20` |
| 3 | `parrot-jump-03-rdp` | `10.61.250.22` |
| 5 | `parrot-jump-p001t001b01` | `10.1.1.125` |
| 6 | `parrot-jump-p001t001b01-ssh` | `10.1.1.125` |
| 7 | `p001t001-jump-iac-01` | `10.1.1.11` |
| 8 | `parrot-jump-p002t001b01` | `10.2.1.11` |
| 9 | `parrot-jump-p003t001b01` | `10.3.1.11` |
| 10 | `parrot-jump-p003sb01` | `10.3.254.11` |

The protected pre-removal backup is
`/home/echo/.local/share/crange/guac-reconciliation/native-connections-before-removal-20261008T212642Z.json`
(mode `0600`). It contains sensitive connection parameters; keep it outside
Git. Controller source backups are under
`/root/.local/share/crange/guac-reconciliation/` in CT 114, also mode `0600`;
the latest pre-deploy source backup is
`bridge-source-before-verified-native-filter-20261008T213444Z/`.
The deployed Guacamole extension backup is
`/opt/guacamole/extensions/crange-theme.jar.20261008T213451Z.bak` in CT 110.

Verification: `tests.test_guac_native_inventory` and
`tests.test_guac_bridge` pass (13 tests), and
`node tests/test_guac_native_inventory.js` passes the stale-tree and
cross-source-ID regression. Python compilation and JavaScript syntax checks
pass. The signed bridge route returned only ID 4 at `10.60.0.20`, mapped to
running controller container 114. Both controller services are active. The
Guacamole extension loaded after restart and its HTTP health check passed. The
deployed `crange-theme.jar` SHA-256 is
`0ea9504cc35c2ff43b3b2935e8c35e6acd129e07aa9d6ba2f72300716ba7e441`.
