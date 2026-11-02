# Browser release gate

The committed Playwright suite runs the production Next.js server behind an
ephemeral local HTTPS proxy against a deterministic, in-process HTTP fixture.
The proxy generates a one-day self-signed localhost certificate at runtime so
browser tests exercise production `Secure` cookies without committing a key.
It does not require PostgreSQL, FastAPI, third-party credentials, or access to a
live provider. OpenSSL must be available on the test host.

From `apps/web`:

```bash
pnpm build
pnpm exec playwright install chromium firefox webkit
pnpm test:e2e
```

The matrix covers Chromium desktop, Chromium at 320×568, a touch-enabled
768×1024 Chromium tablet in dark/reduced-motion mode, Firefox desktop, and
WebKit at 390×844. The 320 CSS-pixel layout also exercises the WCAG 400% reflow
boundary for a 1280-pixel desktop canvas. A dedicated desktop-semantic journey
also resizes the effective CSS viewport to 640 and 320 pixels to verify the 200%
and 400% reflow equivalents, captures the browser accessibility tree, and
checks the complete login landmark/control sequence used by screen readers. It
verifies authentication boundaries,
private-link and security headers, RU/EN public application, assessment and
scheduling flows, keyboard focus, offline/slow-network recovery, responsive
overflow, semantic unavailable states, and zero serious or critical Axe
violations.

It also protects the public branding boundary (same-origin storage proxy versus
direct HTTPS CDN URLs) and the assessment clear-then-submit race by inspecting
the fixture server's ordered PATCH/submit log.

An authenticated, populated interviewer fixture protects permission-driven UI:
candidate, folder, position, pipeline, export, AI, and creation mutations stay
absent while read access remains usable. The same journey verifies direct
position-creation URLs fail closed, including an attempted `?edit=1` bypass on
position detail.

The fixture implements only the upstream contracts used by these browser
journeys. API behavior, tenant isolation, database constraints, and provider
contract tests remain owned by the backend test suites. Failure screenshots,
traces, and the CI HTML report are written below `output/playwright/`, which is
git-ignored and uploaded by CI when the browser gate fails.
