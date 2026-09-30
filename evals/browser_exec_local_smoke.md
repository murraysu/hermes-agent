# Local browser_exec acceptance — 2026-09-30

Runtime: svc1, hermes-agent-sm121, image
`hermes-agent-hermes-agent:fix-20260928-pty-honcho-identity`
(`sha256:77fca9b7de102665370c8f5c211a87f2db36b8e23db6a93dcb0d1631934e231c`).

Two failures were reproduced through browser_exec as the runtime hermes user:

1. Persistent `browser.use_real_profile: true` tried to attach a real default
   Chromium profile that does not exist in this headless container.
2. With real-profile mode disabled, agent-browser could not discover the Chromium
   headless shell packaged under `/opt/hermes/.playwright`.

Fix: persistent `/opt/data/config.yaml` now selects `backend: browser-use` and
`use_real_profile: false`. Compose supplies `AGENT_BROWSER_EXECUTABLE_PATH` for
the packaged executable. This path belongs to the pinned image above; verify and
update it when changing the image's Playwright Chromium build. No new CDP service
or Python Playwright installation was needed. Existing local session machinery
launches the browser and supplies its CDP endpoint to Browser Use CLI.

The config backup is `/opt/data/config.yaml.pre-browser-fix-20260930` (0600).
The named `/opt/data` volume retains config and CLI caches on recreation. The
CLI currently uses Hermes' uvx fallback; this acceptance does not certify a cold,
empty-volume offline installation.

After `docker compose up -d --no-deps --no-build --force-recreate hermes-agent`,
real `hermes-api-server` registry dispatch passed JS rendering, input typing,
coordinate clicking, iframe DOM extraction, and a nonempty screenshot. A separate
public HTTPS navigation passed. API `/health` returned 200. Tests used synthetic
content; no external form was submitted and no real login profile was accessed.

Re-run the deterministic test without a model call:

```bash
docker cp evals/browser_exec_local_smoke.py hermes-agent-sm121:/tmp/browser-smoke.py
docker exec --user hermes --workdir /opt/hermes hermes-agent-sm121 \
  /opt/hermes/.venv/bin/python /tmp/browser-smoke.py
```

Expected: `success: true`, with checks JS/fill/click/iframe/screenshot and marker
`BROWSER_EXEC_LOCAL_OK`. This verifies tool exposure and actual dispatch, not the
model's decision to invoke it or a particular Wix site's completeness.
