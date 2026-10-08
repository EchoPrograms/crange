# Project 100 dashboard acceptance

Mark an item complete only with an actual job ID, runtime observation, or
browser check. Avoid destructive exercises on unrelated resources.

| Area | Required exercise | Evidence/status |
| --- | --- | --- |
| Projects | Create, automatic transit allocation, rename, selection | Partial: live P100 created, five teams declared, transit 10.60.2.2; authenticated project-settings API renamed it to Meridian Harbor Logistics; Chromium login and project selection displayed the new name |
| Templates | Shared ISO/mirror selection, Windows installer, approved images | Partial: official Windows media imported, failed owned builds cancelled, corrected installer reached managed OS; Windows clone publication pending |
| Cluster | Capacity in GiB, commitments, node distribution, suggested nodes | Partial: live CPU reserves and zero template commitments verified; image availability status corrected; lab-specific placement/browser audit pending |
| Staging | Declare/edit/remove VM, container and custom network | Partial: ten Linux jump boxes and four Team 0 CTs declared; Chromium Scenario page rendered desktop/mobile; no staging plan/apply |
| Planning | Foundation review/apply, staging review, retained plan identity | Partial: reviewed foundation applied, 17 additions/0 changes/0 deletions; staging review pending |
| Deployment | Apply and configuration together, refresh after completion | Pending |
| Health | Default ping and explicit SSH/RDP/HTTP checks, edit checks | Pending |
| Teams | Five teams, Team 0, box status and team-scoped management | Partial: Chromium displayed six project networks (Team 0 plus Teams 1–5) desktop/mobile; no team deployment |
| Jump boxes | Custom Windows/Linux profiles, names, placement, protocols | Pending |
| Users | Bulk create, default setup, naming, passwords, box assignments | Partial: ten team-scoped users created via authenticated API; jobs `66505afd239a461eabe351778e507e52`, `34ad9a994efe42afa39d0ad04bf8b901`, `782f16bd8b324942b0c4661520b0e7a9`, `0a1da8113a204ce1a47826624e989b4e`, `f028a1613cca44208bf4f8005a6e28a7` succeeded; Guac API confirmed all ten; Chromium showed all ten desktop in natural team/user order; password-reset and connection grants are disabled |
| Account management | Rename, reset permission, set password, lock/unlock, delete fixture | Partial: API fixture create/rename/password change/reset toggle/lock/unlock/delete verified with jobs `fbd6a3079cf547e9948e67c29530fb18`, `1146ed7e7dab43c8ae7cc32e393e8ee4`, `09aa81362f12401681658f55a15f2930`, `2734debc2a9f4226a12a09c533a393e7`, `92a1aa9f55bf4a26a2c2fa96ea81eabe`, `419a3f04d1234b2e9a2f9bbf742fe0ef`; new password authenticated, old rejected, fixture deleted remotely and locally; browser check pending |
| CRANGE accounts | Separate dashboard accounts and project authorization | Pending |
| Guest accounts | Create/delete, password, sudo grant/revoke on owned fixture | Pending |
| Guacamole | Team/project grouping, IPs, SSH/RDP connections and admin access | Partial: all ten participant accounts verified remotely and team-scoped locally; zero connection grants pending guest deployment; CRUD fixture confirmed remote create/rename/password/reset/disable/delete effects; browser launch pending |
| Power/snapshots | Start, shutdown, stop override, restart, create/revert/delete fixture snapshot | Pending |
| Jobs | Visible task/status, modal, scrolling, notices, stop and recovery | Pending |
| Releases | Capture, fresh-clone test, selected/all-team rollout, upgrade/rollback | Pending |
| Isolation | Cross-team denial, controller/Guac management path, no host exposure | Pending |
| Cleanup | Disposable probes/users/releases removed; necessary bases retained | Pending |

Features that require additional implementation remain pending rather than
being claimed as exercised. The final audit must cover all twenty participant
jump boxes and every deployed team's business systems.
