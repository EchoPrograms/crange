# CRANGE interface direction

CRANGE is an infrastructure operations tool for technical users. Preserve its
monochrome, compact identity. Optimize for scanning real resources, understanding
current state, and completing a task. This document records the owner's design
preferences and the October 19, 2026 interface review.

## Geometry and surfaces

- Use square corners for controls, tables, object cards, dialogs and menus.
  The shared corner tokens are `--radius: 0` and `--cr-radius: 0`.
- A card represents an actual project or server, or a distinct form group.
  Do not wrap every paragraph, statistic and table in another card.
- Use flat grayscale surfaces and restrained separators. Reserve shadows for
  floating UI where overlap needs to be apparent.
- Color operational states in plain text and small indicators: sage for healthy,
  amber for waiting/stopped, muted blue for running work and navigation, red for
  failures. Addresses use pale cyan; protocol icons distinguish SSH/RDP/VNC.
  Never combine colored text, a matching colored outline and a darker tinted fill.
  Controls and panels keep neutral outlines and surfaces. Checkboxes are white.

## Density and hierarchy

- Maintain the compact desktop layout. Use consistent control heights and
  alignment; increase targets for touch without inflating all desktop content.
- Use self-hosted IBM Plex Sans for the interface, following the owner’s latest
  request for a compact, proportional sans-serif face. Keep Plex Mono Regular
  only for machine output and code. Fonts use font-display swap and local fallbacks.
- Keep the body readable at 13px, forms at 12px or greater, and functional metadata
  at least 12px. Do not simulate hierarchy with tiny uppercase labels.
- Establish grouping through proximity, typography and a consistent spacing
  scale. Keep labels closer to their field than the preceding field.
- Give server names and addresses space. Grid and List are alternative views
  of the same authorized resources, not separate inventories.

## Interaction

- Put frequent actions next to the resource: Connect, Edit and current state.
  Put specialist and infrequent actions behind descriptive menus.
- Derive choices that can be inferred from context. Preserve explicit review for
  infrastructure changes and confirmation for destructive actions.
- Use one tooltip implementation, visible keyboard focus, clear loading and error
  states, and honest unknown power status.
- Keep native Guacamole clients, keyboard/IME capture, clipboard, file transfer,
  sharing and account permissions. Never style remote pixels or expose controller
  credentials in presentation components.

## Research and interpretation

These sources support the operational principles; square corners are the owner's
specific direction, not a claim that rounded controls are inherently amateur.

- [Carbon data tables](https://carbondesignsystem.com/components/data-table/usage/)
  describes consistent density, toolbar sizing, inline actions and overflow menus.
- [Carbon spacing](https://carbondesignsystem.com/elements/spacing/overview/)
  explains a shared spacing scale and grouping without unnecessary containers.
- [NN/g visual hierarchy](https://www.nngroup.com/articles/visual-hierarchy-ux-definition/)
  describes guiding attention with scale, contrast and grouping.
- [NN/g progressive disclosure](https://www.nngroup.com/articles/progressive-disclosure/)
  explains how to retain specialist capabilities while keeping frequent tasks clear.

"Vibecoded" is not an objective UX classification. For this product, avoiding that
look means consistent geometry and behavior, task-specific layouts, restrained
styling and complete states rather than a collection of fashionable components.

Font research: [IBM Design Language — Typeface](https://www.ibm.com/design/language/typography/typeface/) and [IBM Plex source](https://github.com/IBM/plex). Font provenance and hashes are recorded in `gui/fonts/README.md`; the OFL license ships beside the fonts.
