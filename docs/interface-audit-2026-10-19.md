# CRANGE and Guacamole interface audit — October 19, 2026

Audit and implementation used the Impeccable audit/polish guidance and Playwright
against the reachable controller and Guacamole services. Lab deployment remains
paused while Proxmox1 requires manual repair. No guest power, storage or networking
configuration was changed for this work.

## Findings and changes

| Priority | Evidence / impact | Resolution |
| --- | --- | --- |
| P1 | Shared input borders `#4b4b4b` on `#181818` measured 2.04:1; fields were difficult to distinguish. | Dedicated control border `#737373` on `#111111`, 3.98:1. Decorative dividers remain subdued. |
| P1 | Dashboard startup swallowed failures and exposed a blank authenticated shell. | Initial loading status, busy state, visible error and Retry. Narrowed navigation ARIA state to navigation buttons. |
| P1 | Guacamole power polling could overlap repeatedly for slow/offline hosts, consuming request slots and delaying connection navigation. | At most three concurrent power reads, per-resource deduplication, a bounded timeout and rotating batches. |
| P2 | Functional text and brand detail descended to 8–10px; disabled controls used whole-element opacity. | Functional metadata raised to at least 11px; explicit readable disabled colors, clearer labels and log type. |
| P2 | Labels sat too near preceding fields; competing blue selection, checks and status colors reduced hierarchy. | Shared form spacing, neutral selection and focus, white checks, semantic state accents. |
| P2 | Mobile project navigation competed with a fixed-width project title; small controls were difficult to target. | Separate project navigation row and 44px controls on touch/narrow layouts; horizontal table scrolling stays local. |
| P2 | Guacamole's native settings and session menu did not share the dashboard's hierarchy. | Grouped preferences, compact session toolbar, floating options/network panels, native advanced tools retained. |
| P2 | Repeated rounded containers and nested panel boundaries conflicted with the owner's preferred tool aesthetic. | Square corner tokens throughout both themes, flattened nested scenario tables, plain semantic status text and small indicators on neutral surfaces. |

No P0 defect was identified in the inspected UI. Existing backend native-inventory
warnings and offline-node status failures were not hidden or treated as successful
verification.

## Expanded owner-requested functionality

- Project cards lead to grouped server cards, with a persisted Grid/List choice.
- Self-hosted IBM Plex Sans provides compact, proportional interface text. Plex Mono Regular is reserved for machine output and code.
- Preferences group Appearance, Session and Account. Presentation preferences are
  local to the browser; account password changes retain native permission checks.
- The session toolbar exposes navigation, disconnect, power, fullscreen, clipboard,
  display scaling, input controls and the authorized team's network addresses.
- Advanced controls retain Guacamole's devices, file transfer, sharing and protocol
  options. No new guest service or management listener is opened.

## Audit assessment

Subjective review scores, each out of 20; these are design assessments, not a
WCAG certification or a backend availability score.

| Area | Baseline | Revised source |
| --- | ---: | ---: |
| Accessibility / legibility | 12 | 17 |
| Performance / waiting behavior | 11 | 15 |
| Theming / visual consistency | 13 | 18 |
| Responsive layout | 12 | 17 |
| Interaction integrity / states | 13 | 17 |

The incumbent design already had a useful compact shell, descriptive actions,
resource-local controls, explicit state labels and well-separated job output.
Those were retained. Project and server cards are justified by actual objects;
they are not used as the default container for every kind of content.

The detector's remaining brand-padding and tiny-body warnings require context:
the brand's flex alignment leaves actual space around its mark, the base body
is 13px and functional metadata is at least 12px. The compact sidebar is an
intentional owner choice, not a defect to automatically redesign.

## Validation and operational limits

- Existing Guacamole selection/loading, login launch, and ticket-storage isolation
  checks pass.
- New workspace tests cover preference validation, native template preservation,
  authorized power payloads, force-stop override behavior and protocol metadata
  arriving after connection creation. Search is bound through the shared list model.
- Browser evidence covers project cards, 36 authorized server entries, Grid/List,
  preferences, native session controls and the authorized staging network map.
- Source contrast measurements and before/after screenshots are in
  `output/playwright/enterprise-audit/` (local QA artifacts, not release content).
- Deployed source and Guacamole JAR have timestamped rollback copies. Guacamole
  was restarted after confirming there were no active remote connections.
- UI inspection does not establish guest service readiness or solve Proxmox1's
  outage. No power or password-changing action was executed during UI QA.

See [the interface direction](interface-design.md) for the owner's geometry,
density and interaction rules and the external design research informing them.

Final deployed verification: the search reduced 36 entries to one matching server;
the session map displayed six authorized staging entries. Loaded font faces were
IBM Plex Sans Regular, SemiBold and Bold, with no JavaScript exceptions. The
project card corner radius measured 0px. Desktop and 390px mobile layouts passed
the overflow checks; native advanced controls and form Escape dismissal worked.


## Follow-up: context, task safety and type readability

- Moved the project selector into the sidebar with project navigation. Open
  Guacamole remains a header action; participant accounts retain project switching.
- Create Project now opens the project-ID form rather than queuing an empty action
  against the selected project. Project settings now edits the display name;
  Default user remains under project Users.
- Project switching clears stale user data immediately, handles failed directory
  reads locally, and ignores late responses for the previous project. Changing
  context closes forms bound to the former project.
- A failed initial load retains Retry; periodic jobs polling starts only after
  the workspace has loaded successfully.
- Replaced the interface monospace with Plex Sans and raised functional 11px
  labels to 12px. Compact control and row heights remain intact.
- Removed both native favicon candidates before adding the custom remote-access
  SVG; the touch icon uses a matching PNG. This prevents a browser selecting the
  remaining Guacamole stock icon.
- Added semantic color to plain health/job states, address text, navigation/action
  icons and protocol symbols. Borders and surfaces remain grayscale. No matching
  colored text/border/tinted-background badges are used.

The isolated browser fixture covered all ten main dashboard views, project
creation and display-name payloads, rapid project-switch response ordering, VM
health JSON validation, multiple remote protocols, inferred staging placement,
job page-size controls, empty projects, startup failure/retry and participant
permissions. It captured three writes to the fixture and zero production writes;
no JavaScript exceptions or mobile page overflow occurred. This verifies UI
behavior and payloads, not the success of infrastructure operations.


Final follow-up deployment verification loaded Plex Sans on both live services,
confirmed one custom SVG favicon plus its matching PNG touch icon, and passed
1440px desktop / 390px mobile overflow checks. Guacamole showed 36 managed
servers; search narrowed them to one. Body and semantic text colors measured
at least 8.2:1 on their respective surfaces; protocol symbols measured at least
6.4:1 on white connection controls. No browser JavaScript exception occurred.

Backend availability is separate: native connection verification still showed
its existing warning, and one live template-catalog read exceeded a 20-second
request budget. Those are not reported as successful backend acceptance. The
managed connection controls remained available; template-view functionality was
covered with the isolated fixture. Proxmox1 repair remains outside this UI task.


### Native inventory and connection-icon follow-up

The repeated native-connection banner was traced to HTTP 595 while reading CT 100
on offline Proxmox1. The inventory treated one unavailable guest as a fatal global
error. It now checks node availability first, isolates individual config failures,
uses cluster resource power state, and gives interactive reads a two-second,
single-attempt budget. Other infrastructure API callers retain their existing
90-second request timeout and safe GET retry behavior. The live authenticated
extension-to-controller request returned HTTP 200 in 0.13 seconds with a warm
cache (three seconds cold), with 36 authorized entries and 33 reachable matches.

The icon correction uses flex cross-axis centering for both link and button
variants. Connection symbols are 16px with heavier strokes and inherit the
button's neutral text color. OS indicators are 19px; server names are 14px in
List, with 13px operational metadata. The larger text retains 32px desktop
connection buttons. Linux/Windows/BSD come from approved/applied or verified
Proxmox OS metadata; an unknown OS retains a generic monitor symbol.

An isolated browser render checked SSH/RDP/VNC and Console alignment, neutral
icon colors, OS masks and 390px mobile overflow. Backend regression checks cover
offline nodes, guests disappearing between reads, genuine cluster failures,
authorization, and the separate interactive timeout/retry policy.
