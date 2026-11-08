# E2E evidence — 2026-11-08

Playwright end-to-end run against `control.py --port 8098` serving the built UI, **rc=0**:

- `result.json` — machine-readable outcome (panels, bench rows, `chat_ok`,
  POST /ctl calls, post-gate console/pageerror counts)
- `run_e2e.py` — the exact run script, including the documented allowlists
  (model-load 503 storm cleared at a double-readiness gate, upstream `/tools`
  probe, `/v1/streams/lookup` version skew)
- `01-control-page.png` — control page: 8 runtimes, model store, bench rows
- `02-activated.png` — after the UI stop/start/activate cycle
- `03-chat.png` — chat streamed through the service proxy (PONG)

Previously lived in `~/.hermes/cache/scratch/` (auto-pruned after 24 h) — moved
here because `README.md` cites this evidence.
