# Fork: llama.cpp + local-AI control (SaidSultani)

## What this is

A fork of [ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp) at the
latest stable tag **v0.9.0** (2026-11-04), used as the base for enhancing the
bundled `llama-ui` web interface (SvelteKit, `tools/ui/`) with the local-AI
control features we need on Snapdragon X Elite hardware.

We fork the FULL repository (not an extracted copy) deliberately: shared
history is what lets us send enhancements back upstream as normal pull
requests against `tools/server/` and `tools/ui/` paths. llama.cpp is MIT —
reuse and modification are explicitly permitted; only the license header has
to travel with the code.

## Why fork at all

`llama-ui` ships a chat interface, but selecting HOW a model runs on this
machine (which backend build, which compute device, or whether GenieX runs it
instead) happens at process start — no UI can expose that without a control
layer. Three llama.cpp builds exist locally (CPU in WSL, OpenCL Adreno on
Windows, Hexagon QNN NPU on Windows) plus the GenieX runtime (v0.10.0, QAIRT
2.50) with its own `cpu|gpu|npu|hybrid` device matrix. Nothing available
today lets you pick among those from one UI.

## Roadmap

### Phase 1 — ours (stays in this fork)

1. **Control service** (small local HTTP service): start/stop llama-server
   builds and `geniex serve` with the right backend flags, expose active
   runtime + device to the UI.
2. **Device/runtime selector page** in llama-ui: CPU / Adreno OpenCL / Hexagon
   QNN / GenieX (llama_cpp or qairt plugin × cpu|gpu|npu|hybrid|auto).
3. **Model store panel**: merge `~/.cache/geniex` (model-manager ids) with
   local GGUF directories; show RAM fit.
4. **Bench panel**: run `geniex-bench` cells and chart TTFT / prefill / decode
   per device. Baseline measured 2026-11-08 (Qwen3-0.6B Q4_K_M:
   cpu 460/22.7, npu 2275/31.1, hybrid 1156/45.6, gpu 873/50.1 t/s
   prefill/decode, HTP0 confirmed on v0.10.0).

### Phase 2 — upstream candidates (we will offer these back)

- Compute-device picker UI backed by `--list-devices` / backend info (generic:
  CUDA, Vulkan, Metal, OpenCL — not our hardware-specific glue).
- Local model-directory manager wired to the server's model-router endpoints.
- In-UI benchmark view fed by the server's existing `/metrics` + slot stats.

GenieX integration and the launcher for our three local build variants stay
in this fork — they are hardware/project-specific and not a fit upstream.

## Building the UI

```bash
cd tools/ui
bun install          # 1060 packages (2026-11-08: 18s)
bun run build        # pwa assets + vite build -> ./dist  (exit 0)
```

The C++ server can also serve this dist directly:
`llama-server --path tools/ui/dist`. Manual builds stamp
`build.json` with `b0000` (CMake normally injects the real version).

## Syncing with upstream

```bash
git remote add upstream https://github.com/ggml-org/llama.cpp.git  # once
git fetch upstream --tags
git merge upstream/v0.9.0   # or the next stable tag, into a new base branch
```
