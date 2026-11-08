# local-control — runtime/device control service (fork Phase 1, ours)

The bundled `llama-ui` chats against an already-running server, but on this
machine the *choice* — which backend build, which compute device, or GenieX
instead of llama.cpp — is made when the process starts. This small stdlib
Python service exposes that choice over HTTP so a UI page can drive it.

**Not planned for upstream** (hardware/project-specific); the llama-ui pages
that consume it are Phase 1 in `../FORK.md`.

## Run

```bash
python3 control.py                 # http://127.0.0.1:8098 — UI + proxy + control
python3 control.py --port 8091 --config runtimes.json
python3 control.py --ui-dir /path/to/dist   # SPA source (default ../tools/ui/dist)
python3 -m unittest test_control.py -v   # suite (fake runtime, no real servers)
```

## API

| Method | Path                        | Meaning                                  |
| ------ | --------------------------- | ---------------------------------------- |
| GET    | `/` , `/health`             | liveness of the service itself           |
| GET    | `/runtimes`                 | inventory + status + health per runtime  |
| POST   | `/runtimes/<id>/start`      | spawn (409 if already running/port busy) |
| POST   | `/runtimes/<id>/stop`       | terminate (409 if not running)           |
| GET    | `/runtimes/<id>/log?tail=N` | last N stdout/stderr lines               |
| POST   | `/runtimes/<id>/activate`   | make a runtime the proxy target (`/ctl/*` aliases exist for all routes) |
| GET    | `/active`                   | active runtime + device                  |
| GET    | `/ctl/models`               | GenieX model cache + GGUF dirs, vocab filter, RAM fit |
| GET/POST | `/bench`                  | bench result list / run (`device`, `model` body) |
| ANY    | `/v1/*`, `/props`, `/slots`, `/metrics` | proxied to the ACTIVE runtime (300 s *inactivity* timeout so slow CPU generation survives) |
| GET    | SPA routes (`/`, `/control`, …) | built UI from `--ui-dir`, `index.html` fallback |

## Inventory (verified 2026-11-08)

| id                 | runtime                          | device    | platform |
| ------------------ | -------------------------------- | --------- | -------- |
| `wsl-cpu-b9189`    | llama.cpp b9189                  | cpu       | WSL      |
| `wsl-all-8247`     | llama.cpp b8247 all-backends     | multi     | WSL      |
| `wsl-vulkan-10066` | llama.cpp b10066 Vulkan          | vulkan    | WSL      |
| `win-cpu`          | llama-cpp-build (Clang ARM64)    | cpu       | Windows  |
| `win-npu-hexagon`  | llama_cpp_hexagon QNN build      | npu       | Windows  |
| `geniex-serve`     | GenieX CLI v0.5.0 serve          | default   | Windows  |
| `geniex-serve-npu` | GenieX CLI serve `-c npu`        | npu       | Windows  |
| `geniex-serve-cpu` | GenieX CLI serve `-c cpu`        | cpu       | Windows  |

Each llama.cpp build is a *different compiled backend* — that is why device
selection is process-level, not an in-process API call. `geniex serve`
computes with `-c cpu|gpu|npu|hybrid` at startup (v0.5.0 help verified).

## Networking caveats (WSL2 <-> Windows)

- WSL2 NAT gives WSL and Windows **different loopbacks**: a server bound to
  Windows `127.0.0.1` is invisible from WSL. Windows runtimes therefore bind
  `0.0.0.0` in their args and use `"health_host": "auto"`, which resolves the
  Windows host IP from the default gateway (`/proc/net/route`).
- If WSL is switched to mirrored networking, `auto` resolves to localhost and
  everything keeps working — the config needs no change.
- Process liveness is authoritative: `status` can be `running` while
  `health` is `false`/`null` — that means "up, but not reachable from here"
  (wrong bind, firewall, or still initializing). Check the log.

## Honest status notes

- `win-npu-hexagon` is **unverified**: comparable QNN builds died at
  session-open `0x72`. Start it and read `/log` — a crash is information.
- The GenieX `-c npu` path crashed inside `geniex_llm_create` on v0.5.0 in
  August; the separate v0.10.0 bench bundle proves HTP0 works (2026-11-08:
  npu prefill 2275 / decode 31.1 t/s). The serve entry exists to learn
  whether v0.5.0 serve differs from v0.5.0 infer.
- `llama-cpp-backends/` (3 more Windows variants incl. `cpu-opencl-built`)
  is not wired in yet — classified variants can be added to
  `runtimes.json` without touching code.

## Verified live smoke — 2026-11-08

Unit suite: 10/10 green (fake runtime, port 8099). Real runtimes through the
service, start -> observe -> stop:

| runtime            | outcome                                                        |
| ------------------ | -------------------------------------------------------------- |
| `wsl-cpu-b9189`    | up, health in 0.5 s, model loads from `/mnt/c/...` cache; `/v1/models` returns 503 **while the model is still loading** (server by design), stop SIGTERM clean |
| `geniex-serve`     | serving `0.0.0.0:18181` ("Local hosting" in log); first NAT health probe said False — the WSL->Windows probe path is flaky (see networking caveats), stop clean |
| `geniex-serve-npu` | **stays up with `-c npu`** — the August crash was `geniex infer`'s llama-create, not serve startup; actual chat inference not yet exercised through serve |
| `win-cpu`          | up, health True, loading `C:\Users\Said\.cache\...` model, stop clean |
| `win-npu-hexagon`  | **fails at HTP session open** (`0x80000406` -> `GGML_ASSERT(device)`, rc=9) — pre-QAIRT-2.50 build; GenieX v0.10.0 proves the hardware path works |

Notes captured in-run: `geniex serve` announces `v0.5.0 -> v0.10.0` via
`geniex update` (official in-place upgrade — no manual path surgery needed).
Windows-executable launching required `exe_for_launch()` translation: WSL
execve needs `/mnt/c/...` as argv0; a raw `C:\\...` raises FileNotFoundError
(this was the original "start returns no response" bug).

## End-to-end proven — 2026-11-08

Playwright run against the service serving the built UI, **rc=0** (evidence:
`~/.hermes/cache/scratch/e2e-local-ai/` — screenshots + `result.json`):

- `/control` page renders: 8 runtime rows, model store, 5 bench rows, sidebar
  "Local AI" entry. The UI runs `router.type: 'hash'`, so the link is
  `#/control` and bare `/control` fetches fall back to the chat home.
- Button cycle start → stop → start → activate through the service: every
  `POST /ctl/runtimes/<id>/*` answered 200 (after a UI stop the status reads
  `exited (rc N)`, not `stopped`).
- Chat: composer in the forked UI → service proxy → `llama-server` (b9189,
  Qwen3-0.6B Q4_K_M) streamed a reply containing `PONG`; post-readiness
  console clean (0 errors / 0 page errors). Allowlists documented in the run
  script: the model-load 503 storm (cleared only after `/v1/models` AND
  `/props` both answer 200 twice), upstream's `/tools` feature probe, and the
  `/v1/streams/lookup` version skew (v0.9.0 UI vs b9189 server, graceful
  fallback).
- Bench through the service: `POST /bench` → `ok: true`, rc=0, 1060 prefill /
  62.5 decode t/s (PowerShell + registry-PATH spawn).
- joint-workbench launcher: the dashboard starts/stops this service on 8098
  via `/api/local-ai/status|ensure|stop` (verified 13/13 cycle on a test
  server).
