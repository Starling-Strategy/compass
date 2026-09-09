# Third-Party Notices

Compass is distributed under the [MIT License](LICENSE). It bundles the
third-party components below, each under its own license. Nothing here changes
the terms those components ship with.

## Fonts

### Inter

`frontend/public/assets/fonts/Inter-VariableFont_opsz,wght.ttf`
`frontend/public/assets/fonts/Inter-Italic-VariableFont_opsz,wght.ttf`

Copyright (c) 2016 The Inter Project Authors (https://github.com/rsms/inter)

Licensed under the SIL Open Font License, Version 1.1. The full license text is
included at [`frontend/public/assets/fonts/OFL.txt`](frontend/public/assets/fonts/OFL.txt).
The OFL requires that this notice and the license accompany the font files.

### Leitura News

`frontend/public/assets/fonts/Leitura News Roman 1.otf`

A commercial typeface (DSType, distributed by Monotype), used for the display
serif treatment defined in `frontend/src/input.css`. It is **not** covered by
this repository's MIT license and is **not** licensed for redistribution or
reuse by third parties.

> **If you fork or redistribute this repository, remove this file and supply
> your own licensed serif**, or confirm your own license with Monotype.
> NCTQ's license covers NCTQ's use of the Compass frontend only.

## JavaScript

Vendored, unminified sources are available from each project. These are
committed as built files rather than resolved through a package manager, so
they are **not** covered by Dependabot and must be updated by hand.

| Component | File | License |
| --- | --- | --- |
| DOMPurify | `frontend/public/assets/vendor/purify-*.min.js` | Apache-2.0 / MPL-2.0 |
| marked | `frontend/public/assets/vendor/marked-*.min.js` | MIT |
| Chart.js | `frontend/public/assets/vendor/chart-*.umd.min.js` | MIT |
| MSAL.js | loaded from the Microsoft CDN | MIT |

## Package dependencies

Python dependencies are pinned in `backend/uv.lock` and
`dashboard/requirements-dashboard.txt`. PHP dependencies are pinned in
`frontend/composer.lock`; Node build tooling in `frontend/package-lock.json`.
Each package's license is recorded in its own distribution metadata.

## NCTQ content and trademarks

Policy content under `backend/content/nctq-policy/`, the documentation in
`docs/`, and NCTQ's names, logos, and brand assets are the property of the
National Council on Teacher Quality. The MIT license covers the software in
this repository; it does not grant rights to NCTQ's content or trademarks.
