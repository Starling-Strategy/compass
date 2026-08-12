# Pathfinder visual and embed specification

This is the implementation-facing reference for the Compass experience embedded
in the NCTQ District Policy Pathfinder. It explains the boundary between the
Pathfinder page and the Compass iframe, records the behavior currently implemented
in the repository, and identifies decisions that still need confirmation.

This file is not a replacement for an approved NCTQ design-system or Figma
specification. No separate visual authority was present in the repository when
this was written. If NCTQ design documentation becomes authoritative, link it in
the Design authority row below and update this page when the implementation
changes.

## Design authority

| Item | Current reference |
| --- | --- |
| Product/design authority | **To be confirmed:** link the approved NCTQ Pathfinder or Compass visual specification here |
| Compass implementation | [frontend/public/index.php](../frontend/public/index.php), [frontend/src/input.css](../frontend/src/input.css), and [frontend/public/assets/js/embed.js](../frontend/public/assets/js/embed.js) |
| Technical integration explanation | [docs/08-technical-reference.md](../docs/08-technical-reference.md#pathfinder-integration) |
| Parent-page responsibility | The NCTQ Pathfinder page that embeds Compass owns the iframe element, surrounding layout, and parent-side handling of Compass messages |

Until an approved design source is linked, the frontend implementation is the
baseline for what the embedded experience currently does. It is not, by itself,
proof that every visual choice is an intentional long-term requirement.

## What Pathfinder owns and what Compass owns

| Concern | Pathfinder parent page owns | Compass iframe owns |
| --- | --- | --- |
| Placement | Where the iframe appears and what surrounds it | The chat interface inside the frame |
| Width | The iframe's available width and any outer page constraints | Internal max-width and responsive layout within the available width |
| Height | Applying the height reported by compass:resize and choosing a safe fallback | Reporting document height as content changes |
| Parent-page context | Page title, surrounding explanatory copy, and fallback if the frame cannot load | The Compass heading, prompt, conversation, citations, and beta notice |
| Prompt injection | Sending a validated compass:prompt message | Accepting and displaying the prompt, and optionally submitting it |
| Visitor continuity | Generating and sending a pseudonymous visitor ID when the product uses one | Preferring the parent-issued ID and falling back to a local ID |
| Cross-origin security | Sending messages only to the intended Compass origin | Validating incoming message origins and treating outgoing payloads as untrusted by the parent |
| Visual consistency | Ensuring the iframe has enough space and does not conflict with Pathfinder navigation | Applying Compass typography, colors, controls, loading states, tables, charts, and source panels |

## Embed mode

Pathfinder embeds Compass by adding the query parameter embed=true to the
Compass frontend URL.

In embed mode, the frontend:

- adds the embed-mode body class;
- removes the full-page Compass sidebars;
- keeps the chat, prompt form, citations, charts, exports, and beta notice;
- shows an Open full version link so a user can leave the embedded context;
- keeps the viewport meta tag and responsive layout behavior.

The current implementation is in
[frontend/public/index.php](../frontend/public/index.php). The Apache frame
policy is configured in the frontend container build and is documented in
[§8](../docs/08-technical-reference.md#pathfinder-integration).

Pathfinder should give the iframe a stable, visible region in the page and should
not assume that one fixed height will work for every answer. Long tables,
streamed responses, citations, charts, and document panels can all change the
required height.

## Resize contract

Compass sends:

    { type: "compass:ready", payload: { version: "1.0" } }

when embed messaging initializes.

It then sends:

    { type: "compass:resize", payload: { height: <document scroll height> } }

after content changes. The implementation debounces resize notifications by
100 milliseconds because streaming and chart animation can cause several layout
changes in quick succession.

Pathfinder should:

1. listen only for messages from the expected Compass origin;
2. verify the message type and that height is a finite, sensible positive value;
3. apply a minimum height and a reasonable maximum or overflow policy;
4. update the iframe height without creating a nested page scrollbar;
5. retain a fallback height if resize messages are delayed or unavailable; and
6. avoid sending a height message back in response to every resize unless the
   integration explicitly needs one.

The Compass implementation observes the document body with ResizeObserver.
The parent must still treat the payload as untrusted input.

## Prompt and visitor-ID messages

Pathfinder may send:

    { type: "compass:prompt", payload: { text, autoSubmit } }

to place a question in the Compass input. The parent should validate the text,
apply its own length and content rules, and set autoSubmit deliberately.

Pathfinder may send:

    { type: "compass:visitor_id", payload: { visitor_id } }

when it has a pseudonymous first-party visitor identifier. The identifier must
not contain a person's name, email address, or other identifying information.
Compass caps the received value and prefers it over its iframe-local fallback.

Compass accepts messages from configured NCTQ origins and known NCTQ subdomains.
The allowed-origin list is implementation configuration, not a substitute for
the parent validating where it sends messages.

## Current visual baseline

The exact CSS values remain in the source files. The current implementation
establishes these broad behaviors:

### Typography

- Inter is the primary sans-serif interface font.
- Leitura News is used for the display serif treatment.
- Fonts are vendored and loaded from the frontend so a partner firewall does
  not make the interface depend on Google Fonts or a CDN.
- Answer text, controls, citations, tables, and source panels use the frontend's
  existing typography classes and CSS.

### Layout and responsive behavior

- The embedded page removes the full-page sidebars.
- The main content is centered with a bounded readable width.
- The prompt form and conversation use responsive spacing and controls.
- Mobile layouts use smaller typography and compact controls at the existing
  frontend breakpoints.
- Chat content can scroll internally while the page remains usable.
- The parent page must allow enough width for tables and charts; the iframe should
  not be placed in a narrow column without an agreed overflow behavior.

### Color and contrast

- Compass uses the existing NCTQ/Compass navy, blue accent, light text, muted
  text, and white control treatments.
- Citation links, focus indicators, blockquotes, source panels, and status
  elements use the existing accent and contrast styles.
- Exact color tokens and contrast-sensitive values belong in
  [frontend/src/input.css](../frontend/src/input.css), not in a second
  hand-maintained table here.
- A visual review should confirm contrast against the actual Pathfinder
  background and any surrounding cards or borders.

### Loading and streaming

The interface can show an activity indicator while a response is being prepared
and streamed. Pathfinder should keep the iframe visible during loading and should
not replace a legitimate in-progress response with a parent-page spinner unless
the parent has explicitly chosen that experience.

### Errors and degraded states

Compass has in-frame error and document-viewer states. The parent page should
provide a fallback for an iframe that fails to load, cannot be resized, or loses
network access. The fallback should explain that the chat is temporarily
unavailable and provide the agreed next action, such as opening the standalone
Compass experience or contacting NCTQ.

### Tables, charts, citations, and documents

Answers may contain:

- data tables with source markers;
- CSV or other download controls;
- charts;
- expandable source/citation panels;
- document-viewer overlays.

The parent layout must not clip these controls. The resize contract should be
tested with a long table, a chart, an expanded citation panel, and the document
viewer open.

## Accessibility expectations

The embedded experience should be reviewed as a complete user flow, not only by
checking individual HTML attributes.

Current implementation signals include:

- a labeled conversation log with role=log and polite live updates;
- labels for the prompt, send, stop, sidebar, fullscreen, and download controls;
- visible focus styles for interactive source items;
- keyboard-operable buttons and form controls;
- readable contrast choices for small beta and status text.

The Pathfinder integration should additionally:

- give the iframe a meaningful title;
- preserve keyboard focus when the iframe resizes or the parent updates layout;
- avoid trapping keyboard focus in the iframe or its fallback;
- provide a usable fallback if scripting or the iframe is blocked;
- test zoom and narrow viewport behavior;
- test screen-reader announcements during streaming;
- ensure charts and tables have understandable labels or adjacent summaries; and
- confirm that the parent page's heading structure still makes sense around the iframe.

These are expectations to verify. They are not a claim that every item has already
passed an accessibility audit.

## Review checklist

Before calling the Pathfinder integration visually complete, confirm:

- [ ] An approved NCTQ/Pathfinder visual authority is linked above.
- [ ] The iframe has a meaningful accessible title.
- [ ] The parent applies compass:resize safely and avoids nested scrollbars.
- [ ] A minimum/fallback height is defined.
- [ ] Landing, loading, streaming, completed, long-table, chart, citation-panel,
      document-viewer, error, and unavailable states have been reviewed.
- [ ] Mobile, tablet, desktop, zoom, and narrow-column layouts have been reviewed.
- [ ] Typography, colors, spacing, and control treatments match the approved design.
- [ ] Keyboard and screen-reader behavior has been tested.
- [ ] The standalone-link and parent-page fallback behavior are agreed.
- [ ] The allowed Pathfinder origins and message payloads are documented and tested.
- [ ] The owner and review date for this specification are recorded.

## Open decisions

- What is the approved external visual-design source for the Pathfinder embed?
- Should the parent page set a fixed minimum height, or should it use a product-level
  fallback until the first compass:resize message?
- What should users see if Compass is unavailable inside Pathfinder?
- Which team owns visual regression review when Compass or Pathfinder changes?
- Should charts, tables, and document panels have additional parent-page constraints
  for small screens?
- What accessibility standard and review cadence should govern the embed?
