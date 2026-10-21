# Maintained template publication

Administrator base publication uses fixed source IDs on Proxmox4. Sources remain stopped and immutable. A full disposable derivative is isolated on `crprobe1450` with every NIC disconnected, booted, checked for normal service readiness, generalized, and exported. An independent clone must boot with a different machine ID and SSH host key before publication. Local linked-clone caches pass a separate boot, identity, backing and cleanup check before becoming selectable.

## Repairs

- Capacity discovery restarts once after a proven guest move, so HA relocation cannot omit or double-charge commitments.
- Temporary API capacity errors retry reads and observation of the original provider task only. Mutations are never replayed.
- Maintained Debian and Ubuntu VM bases may test a 1 GB / 1 core verification profile on smaller nodes. This remains a proposal until the real clone boot, unique identity and cleanup pass; deployment sizes remain user-configured.
- A terminal container restore failure caused by temporary library activation can release its exact failed reservation only after fresh node inventory, local-volume absence, task status and archive-pin checks. A reused VMID on another node is preserved; an uncertain task or remaining volume blocks recovery.
- Capture and cache storage preferences fall back to another approved pool only before allocation. Memory, CPU, disk, quota, node admission, ownership and snapshot requirements still apply. An uncertain allocation retains its exact journal and reservation.
- Disconnected systemd probes may clear only a sole `systemd-networkd-wait-online.service` failure. No services are masked or disabled, and subsequent readiness must be normal. Other failed units remain errors.
- Debian 13 derivatives enable LXC nesting while retaining unprivileged isolation, matching the scenario-container module. This addresses systemd credential/mount failures on current PVE hosts.
- Alpine derivatives resolve the installed OpenRC interpreter and install persistent per-clone machine-ID initialization before host-key startup.
- Container generalization removes SSH host keys as well as machine IDs. Existing startup services generate fresh keys on the independent clone.
- Ubuntu container publication advances to version 4; the earlier captured version failed its unique-key gate and remains unpublished. Its evidence is retained.
- Interrupted Linux cache verification can resume the same stopped, owned probe after checking its original task, archive pin and capacity reservation; it does not restore another copy.
- Projects gain a base alias after the first verified local cache exists. Deleted projects retain their configuration without receiving new aliases. Scenario release images remain excluded from base selection, and superseded aliases remain available to existing guest definitions.

## Node policy

Proxmox1 remains reserved for routers. Proxmox4 remains the required storage/quorum core and source reader, and is not silently enrolled for application placement. Proxmox2, Proxmox3, Proxmox5 and Proxmox6 may receive application caches subject to measured capability and available capacity. A full node cannot accept an additional probe or guest merely because it is online.

## Validation

The repair suite passed 1,220 tests with one existing skip before final live publication acceptance. Live image/cache results are recorded separately when acceptance completes.

Proxmox documents the systemd nesting requirement in its [container documentation change](https://lists.proxmox.com/pipermail/pve-devel/2025-October/076158.html).

## Live acceptance

The dashboard exposes five VM choices (Alpine, Parrot, Debian 13, Windows Server 2025 Desktop Experience, and Ubuntu 24.04) and four container choices (Alpine, Debian 12 stock, Debian 13, and Ubuntu 24.04). Each selectable choice has a published logical image ID and passes a read-only placement plan against real cluster inventory, existing reservations and Project 100 staging-network requirements. The authenticated projects API returned HTTP 200 in 0.02 seconds.

The five repaired maintained Linux bases passed independent fresh-clone readiness, unique machine-ID and SSH host-key checks. Ready local caches were also checked for actual Proxmox ownership, stopped state and immutable-template status. See `platform-template-acceptance.json` for the final observed replica matrix and proof summary.

Debian 13 and Ubuntu 24.04 VM caches are available on Proxmox2, Proxmox5 and Proxmox6. Proxmox3's approved local pool lacks the disk headroom needed for their cache plus disposable verification peak; its admission remains closed rather than overcommitting physical capacity. This does not block automatic placement on the other workers. The router-only restriction on Proxmox1 and existing source/core role of Proxmox4 remain unchanged.

The legacy unversioned Windows creation alias is superseded by the verified Server 2025 base. Its original reference remains intact for existing guests and pinned releases. New scenario release images remain available through release workflows and are not OS base choices.
