# Frontend agent guide

Scoped guidance for the PHP/Apache chat UI and server-side API proxy.
[Root guidance](../AGENTS.md) governs shared security and approvals.

## Commands

Run from `frontend/`. The Dockerfile uses Node 20 and PHP 8.3 with cURL;
PHP dependencies use Composer.

```bash
npm ci
composer install
npm run build-css
npm test
npm run dev
```

`npm run dev` serves `public/` at http://localhost:3000; it does not start
the backend. Configure approved backend access server-side before testing chat.
Use `npm run watch-css` separately for CSS iteration. There is no generic
`npm run build` or lint script.

## Source boundaries and pitfalls

- `public/index.php` and adjacent PHP partials define the page;
  `public/api/` proxies backend requests. Keep `FASTAPI_API_TOKEN` server-side.
- Browser ES modules live in `public/assets/js/`. Preserve relative `.js`
  imports and its nested `package.json` module scope.
- `src/input.css` and `tailwind.config.js` own CSS;
  `public/assets/css/index.css` is generated. Rebuild, never hand-edit it;
  review generated diffs and safelist dynamically assembled Tailwind classes.
- Preserve SSE framing, unbuffered PHP forwarding, cancellation, and
  truncated-stream recovery in `public/api/stream.php` and
  `public/assets/js/agentSSE.js`. Test errors and early disconnects too.
- Preserve DOMPurify sanitization, citations, exports, and keyboard/focus handling.
  Copy may have both PHP and responsive/reset JavaScript owners: inspect both.

## Verification

`npm test` runs `tests/*.test.mjs`. These cover welcome copy and the demo
modal, not full chat/proxy integration. Add focused regression tests.
Run PHP syntax checks on changed templates, for example `php -l public/index.php`.
Build the production-shaped image from this directory:

```bash
docker build -t compass-fe .
docker run --rm --name compass-fe-check -p 127.0.0.1:3067:80 compass-fe
```

Check for an existing container/port before starting; do not replace another
session's environment. Check HTTP, rendered desktop/mobile layout, keyboard
interaction, console errors, and the changed behavior. PHP's development server
does not reproduce Apache's SSE/security/cache headers. A working landing page
does not prove backend chat works. Report those checks separately.

Docs and tests inside `frontend/` are deployment-triggering changes on `main`.
Use the shared [release runbook](../docs/06-hosting-deployment-security.md).
