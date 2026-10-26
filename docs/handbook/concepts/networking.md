# Network topology and isolation

The shared management/access network and project/team networks have different purposes. Team 0 is a team LAN for authoring, not unrestricted project transit.

## Address plan

| Object | Address / name |
| --- | --- |
| Shared access | `10.60.0.0/24` |
| Controller in the current installation | `10.60.0.20` |
| Guacamole in the current installation | `10.60.0.10` |
| Shared project-router transit | `10.60.2.0/24`, VNet `crtrans` |
| OPNsense shared-transit interface | `10.60.2.1` |
| Project P aggregate | `10.P.0.0/16` |
| Project P internal transit | `10.P.0.0/24`, VNet `pNNNtr` |
| Project router internal side | `10.P.0.1/24` |
| Team T LAN | `10.P.T.0/24`, VNet `pNNNtNNN` |
| Team router LAN | `10.P.T.1/24` |
| Regular team router transit, absent preserved exceptions | `10.P.0.(T+1)/24` |
| Team 0 staging LAN | `10.P.254.0/24`, VNet `pNNNt254` |
| Static scenario addresses | `.2–.100` |
| DHCP clients | `.101–.254` |

The project router's shared-transit address is separately allocated. Team 0 needs a free project-transit address; do not blindly derive a colliding one. Existing assignments are preserved. Custom networks use their own unused project subnet IDs and belong to one team/staging environment.

Proxmox SDN uses the `crange` zone/VXLAN infrastructure. VNet names identify ownership; names alone are not execution/deletion authorization.

## Traffic path

A team's LAN routes through its team router to the project router and then the shared transit/edge. Custom network routers sit below their owner. OPNsense has a project aggregate route, `10.P.0.0/16` via that project's router. No separate staging route is needed at OPNsense when the project router has the correct return route.

The project router must know every downstream team/staging/custom subnet. Generated inventory supplies those routes and Ansible persists them. Configure project routers before downstream routers because SSH to them is proxied through the project router.

## Policy ownership

Debian routers use UFW with default routed deny and explicit intended egress/management permissions. Team/staging traffic must not gain access to other teams, project transit or internal management networks simply because it belongs to the same project. DNS and DHCP behavior must remain intact.

Project automation manages OPNsense routes/gateways only. **It does not edit OPNsense firewall rules.** Static edge policy is an administrator responsibility independent of project lifecycle.

Management access is scoped to required controller/Guacamole sources and guest services. For the existing Windows baseline, RDP is sourced from Guacamole `10.60.0.10` and WinRM from controller `10.60.0.20`.

## Acceptance is scoped

Network verification creates a protected disposable probe, checks fixed paths/isolation and confirms cleanup before recording evidence. Admission is bound to the reviewed project/network/node/image scope. A node being online or having a similarly named bridge does not establish acceptance. Missing/stale evidence blocks placement instead of silently widening access.
