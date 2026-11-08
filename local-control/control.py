#!/usr/bin/env python3
"""Local-AI control service — llama.cpp fork, Phase 1 (ours).

One HTTP entry point for local AI work:
- start/stop/activate the local inference runtimes (llama.cpp builds and
  GenieX serve) and read their logs (the runtime/device selector backend);
- serve the forked llama-ui build as an SPA (`/`, `--ui-dir`) so ONE url
  always shows the same UI;
- PROXY llama-server API paths (`/v1/*`, `/props`, `/slots`, ...) to the
  ACTIVE runtime, so chat streams through whichever backend was selected;
- model store (`/ctl/models`) and bench results/runs (`/ctl/bench`).

Runs in WSL; Windows executables are launched through the normal WSL interop
layer (WSL executes *.exe paths natively).

Deliberate MVP choices:
- stdlib only (http.server + subprocess + http.client): no deps to break.
- Process liveness is the authoritative health signal; the TCP probe is a
  best-effort extra because Windows-hosted servers are not reachable at
  127.0.0.1 from WSL2 (separate loopbacks). health_host "auto" resolves the
  Windows host via the default gateway and requires the runtime to bind
  0.0.0.0 in its args.
- One process per runtime id; starting twice is refused (409), never silently
  replaced, so ports cannot be double-bound by surprise.
- Control routes win over proxy/static (explicit list below), `/models` GET is
  the SPA page while POST/DELETE proxy to the active server (llama router API).

Usage: python3 control.py [--port 8098] [--config runtimes.json] [--ui-dir DIR]
"""
from __future__ import annotations

import argparse
import http.client
import json
import mimetypes
import os
import re
import socket
import subprocess
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

MAX_LOG_LINES = 500  # ring buffer per runtime — enough for a crash traceback

# Paths that always belong to the ACTIVE runtime (llama-server / geniex API),
# never to static files or SPA routes. '/models' is method-dispatched instead.
PROXY_PREFIXES = (
    "/v1/", "/v1", "/props", "/slots", "/metrics", "/tools", "/cors-proxy",
    "/completion", "/infill", "/apply-template", "/embeddings", "/tokenize",
    "/detokenize", "/templates", "/set", "/advices", "/regenerate",
)

STATIC_SKIP = ("/ctl", "/runtimes", "/health", "/models", "/bench", "/active")


def exe_for_launch(arg: str) -> str:
    """Translate a Windows-style executable path for the WSL exec layer.

    WSL runs Windows .exe files through binfmt, but execve needs a REAL
    Linux path as argv0: a raw 'C:\\...' string raises FileNotFoundError.
    Args the Windows process consumes itself (model UNC paths, --host) are
    passed through untouched — only the executable is translated.
    """
    m = re.match(r"^([A-Za-z]):\\", arg)
    if os.name == "nt" or not m:
        return arg
    return "/mnt/" + m.group(1).lower() + "/" + arg[3:].replace("\\\\", "/").replace("\\", "/")


class RuntimeState:
    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.started_at: float | None = None
        self.exit_code: int | None = None
        self.lines: deque[str] = deque(maxlen=MAX_LOG_LINES)


def gateway_ip() -> str | None:
    """Windows host IP as seen from WSL2 NAT mode (default route interface)."""
    try:
        for line in Path("/proc/net/route").read_text().splitlines()[1:]:
            fields = line.split()
            if fields[1] == "00000000":  # default route target is hex-encoded
                ip_hex = fields[2]
                if len(ip_hex) == 8:
                    b = bytes.fromhex(ip_hex)
                    return ".".join(str(x) for x in reversed(b))
    except (OSError, ValueError):
        return None
    return None


def tcp_probe(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def mem_available_mb() -> int:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


class Service:
    def __init__(self, config_path: Path) -> None:
        cfg = json.loads(config_path.read_text())
        self.runtimes: dict[str, dict] = {rt["id"]: rt for rt in cfg["runtimes"]}
        self.state: dict[str, RuntimeState] = {rid: RuntimeState() for rid in self.runtimes}
        self.model_dirs: list[str] = cfg.get("model_dirs", [])
        self.bench: dict = cfg.get("bench", {})
        self.service_dir = config_path.parent
        self.active_rid: str | None = None

    # ---- process control -------------------------------------------------
    def start(self, rid: str) -> dict:
        rt = self.runtimes.get(rid)
        if rt is None:
            raise KeyError(rid)
        st = self.state[rid]
        if st.proc is not None and st.proc.poll() is None:
            raise RuntimeError("already running")
        port = rt.get("health_port")
        if port:
            host = self._health_host(rt)
            if host and tcp_probe(host, port):
                raise RuntimeError(f"port {port} already in use (foreign process?)")

        env = dict(os.environ)
        # DLL lookup order matters: runtime dirs must come BEFORE anything stale
        for extra in rt.get("env_path", []):
            env["PATH"] = extra + ":" + env.get("PATH", "")

        proc = subprocess.Popen(
            [exe_for_launch(a) if i == 0 else a for i, a in enumerate(rt["args"])],
            cwd=rt.get("cwd") or None,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
        )
        st.proc, st.started_at, st.exit_code = proc, time.time(), None
        st.lines.clear()
        threading.Thread(target=self._pump, args=(rid, proc), daemon=True).start()
        return {"ok": True, "pid": proc.pid}

    def stop(self, rid: str) -> dict:
        rt = self.runtimes.get(rid)
        if rt is None:
            raise KeyError(rid)
        st = self.state[rid]
        if st.proc is None or st.proc.poll() is not None:
            raise RuntimeError("not running")
        st.proc.terminate()  # SIGTERM / TerminateProcess — graceful first
        try:
            st.proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            st.proc.kill()
            st.proc.wait(timeout=5)
        if self.active_rid == rid:
            self.active_rid = None
        return {"ok": True, "exit_code": st.proc.returncode}

    def activate(self, rid: str) -> dict:
        """Select the runtime the API proxy targets. Requires a passing TCP
        probe — the port may belong to a server the user started by hand,
        which is just as valid as one this service spawned."""
        rt = self.runtimes.get(rid)
        if rt is None:
            raise KeyError(rid)
        if not rt.get("health_port") or not tcp_probe(self._health_host(rt), rt["health_port"]):
            raise RuntimeError("runtime not reachable (start it first?)")
        self.active_rid = rid
        return {"ok": True, "active": rid, "api_base": self.active_api_base()}

    def active_api_base(self) -> str | None:
        if not self.active_rid:
            return None
        rt = self.runtimes[self.active_rid]
        port = rt.get("health_port")
        return f"{self._health_host(rt)}:{port}" if port else None

    def active_state(self) -> dict:
        rt = self.runtimes.get(self.active_rid or "")
        health = None
        if rt and rt.get("health_port"):
            health = tcp_probe(self._health_host(rt), rt["health_port"])
        return {
            "id": self.active_rid,
            "api_base": self.active_api_base(),
            "kind": rt.get("kind") if rt else None,
            "health": health,
        }

    def _pump(self, rid: str, proc: subprocess.Popen) -> None:
        st = self.state[rid]
        assert proc.stdout is not None
        for line in proc.stdout:
            st.lines.append(line.rstrip("\n"))
        proc.wait()
        st.exit_code = proc.returncode
        st.lines.append(f"[control] process exited rc={proc.returncode}")

    # ---- status ----------------------------------------------------------
    def _health_host(self, rt: dict) -> str:
        host = rt.get("health_host", "127.0.0.1")
        if host == "auto":
            # Windows loopbacks are NOT visible from WSL2 — probe the host IP,
            # which only answers if the runtime binds 0.0.0.0 (see README).
            host = gateway_ip() or "127.0.0.1"
        return host

    def snapshot(self) -> dict:
        out = []
        for rid, rt in self.runtimes.items():
            st = self.state[rid]
            live = st.proc if (st.proc is not None and st.proc.poll() is None) else None
            running = live is not None
            status = "running" if running else ("exited" if st.proc else "stopped")
            health = None
            if running and rt.get("health_port"):
                health = tcp_probe(self._health_host(rt), rt["health_port"])
            out.append({
                "id": rid,
                "label": rt.get("label", rid),
                "kind": rt.get("kind"),
                "device": rt.get("device"),
                "platform": rt.get("platform"),
                "status": status,
                "pid": live.pid if live else None,
                "exit_code": st.exit_code,
                "uptime_s": round(time.time() - st.started_at, 1) if (live and st.started_at) else None,
                "health": health,
                "health_port": rt.get("health_port"),
                "active": rid == self.active_rid,
                "notes": rt.get("notes"),
            })
        return {"runtimes": out, "active": self.active_rid, "time": time.time()}

    # ---- model store (M2) ------------------------------------------------
    def models(self) -> dict:
        """Merge model dirs into one store with RAM fit. GenieX's cache dirs
        carry a geniex.json sidecar; vocab test fixtures are never models."""
        avail_mb = mem_available_mb()
        rows = []
        for d in self.model_dirs:
            root = Path(d)
            if not root.exists():
                continue
            for p in sorted(root.rglob("*.gguf")):
                if not p.is_file() or "vocab" in p.name.lower():
                    continue
                size = p.stat().st_size
                rows.append({
                    "name": p.stem,
                    "file": p.name,
                    "path": str(p),
                    "size_mb": round(size / 1e6, 1),
                    "dir": str(root),
                    "ram_fit_pct": round(size / (avail_mb * 1e6) * 100, 1) if avail_mb else None,
                })
        rows.sort(key=lambda r: -r["size_mb"])
        return {"models": rows, "mem_available_mb": avail_mb, "dirs": self.model_dirs}

    # ---- bench (M3) ------------------------------------------------------
    def bench_list(self) -> dict:
        results = []
        rdir = self.service_dir / self.bench.get("results_dir", "bench-results")
        if rdir.exists():
            for p in sorted(rdir.glob("*.json"), reverse=True):
                try:
                    data = json.loads(p.read_text())
                except (OSError, ValueError):
                    continue
                agg = data.get("agg", {})
                results.append({
                    "file": p.name,
                    "device": data.get("device"),
                    "device_id": data.get("device_id"),
                    "model": data.get("model_id"),
                    "ttft_ms": (agg.get("ttft_ms") or {}).get("median"),
                    "prefill_tps": (agg.get("prefill_tps") or {}).get("median"),
                    "decode_tps": (agg.get("decode_tps") or {}).get("median"),
                })
        return {"results": results, "devices": self.bench.get("devices", [])}

    def bench_run(self, device: str, model: str | None = None) -> dict:
        exe = self.bench.get("exe")
        if not exe:
            raise RuntimeError("bench.exe not configured")
        if device not in self.bench.get("devices", []):
            raise RuntimeError(f"device not in {self.bench.get('devices')}")
        rdir = self.service_dir / self.bench.get("results_dir", "bench-results")
        rdir.mkdir(exist_ok=True)
        out = rdir / f"run-{device}-{time.strftime('%Y%m%d-%H%M%S')}.json"
        # Bench exe is a WINDOWS process: (1) it only writes Windows paths —
        # wslpath translates ours (UNC); (2) it is a GUI-subsystem binary whose
        # dlls live in <bundle>\lib (NOT next to the exe), so the child must get
        # a PATH that resolves them — the WSL-derived PATH lacks the bundle and
        # the loader then binds the v0.5.0 CLI's stale geniex.dll -> exit
        # 0xC0000139 (ENTRYPOINT_NOT_FOUND) with NO console output. Rebuilding
        # PATH from the Windows registry inside PowerShell is the exact pattern
        # that produced this morning's four device cells.
        try:
            out_win = subprocess.run(
                ["wslpath", "-w", str(out)], capture_output=True, text=True,
                timeout=15, check=True).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            out_win = "\\\\wsl.localhost\\Ubuntu" + str(out).replace("/", "\\")
        exe_dir = exe.rsplit("\\", 1)[0]
        model_id = model or self.bench.get("default_model", "")
        ps1 = (
            "$env:Path=[Environment]::GetEnvironmentVariable('Path','Machine')+';'"
            "+[Environment]::GetEnvironmentVariable('Path','User'); "
            f"Set-Location '{exe_dir}'; "
            f"& '{exe}' --plugin llama_cpp --device {device} "
            f"-m '{model_id}' -n 32 -r 1 --output-json '{out_win}'; "
            "Write-Output ('RC=' + $LASTEXITCODE)"
        )
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps1],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=300)
        if not out.exists():
            tail = ((r.stdout or "") + (r.stderr or "")).strip()[-800:]
            raise RuntimeError(
                f"bench produced no result (transport rc={r.returncode}): "
                f"{tail or chr(40) + chr(39) + chr(39) + chr(41)}")
        data = json.loads(out.read_text())
        agg = data.get("agg", {})
        return {
            "ok": True, "file": out.name, "device": device,
            "ttft_ms": (agg.get("ttft_ms") or {}).get("median"),
            "prefill_tps": (agg.get("prefill_tps") or {}).get("median"),
            "decode_tps": (agg.get("decode_tps") or {}).get("median"),
            "rc": r.returncode,
        }


class Handler(BaseHTTPRequestHandler):
    service: Service  # set in main()
    ui_dir: Path | None = None  # set in main()

    # ---- plumbing --------------------------------------------------------
    def log_message(self, format: str, *args) -> None:  # quiet stdout noise
        pass

    def _headers(self, code: int, ctype: str, length: int | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, Accept")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")

    def _json(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload, indent=1).encode()
        self._headers(code, "application/json", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def do_OPTIONS(self) -> None:  # CORS preflight (cross-origin UIs)
        self._headers(204, "text/plain", 0)
        self.end_headers()

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    # ---- routing ---------------------------------------------------------
    def _dispatch(self, method: str) -> None:
        url = urlparse(self.path)
        path = url.path
        # canonical /ctl/* aliases share the root routes (workbench + UI both work)
        ctl = path.startswith("/ctl/")
        route = path[4:] if ctl else path
        parts = [p for p in route.split("/") if p]
        try:
            if self._control_route(method, route, parts, url, ctl):
                return
            if self._proxyable(method, path):
                self._proxy(method, url)
                return
            if method == "GET":
                if self._static(path):
                    return
            self._json({"error": "not found"}, 404)
        except KeyError:
            self._json({"error": "unknown runtime"}, 404)
        except RuntimeError as exc:
            # "already running" / "port busy" / "not running" — client mistakes,
            # reported with 409 so callers can distinguish from transport errors
            self._json({"error": str(exc)}, 409)
        except subprocess.TimeoutExpired as exc:
            self._json({"error": f"operation timed out: {exc}"}, 504)
        except (OSError, http.client.HTTPException) as exc:
            self._json({"error": f"upstream error: {exc}"}, 502)

    def _control_route(self, method: str, route: str, parts: list, url, ctl: bool) -> bool:
        """Explicit control-plane routes. Returns True when handled."""
        svc = self.service
        if route in ("/health",) and method == "GET":
            self._json({"ok": True, "service": "local-control"})
            return True
        if route in ("/",) and method == "GET":
            # kept for tooling that probed the old root JSON; the SPA takes '/'
            # only when a UI build is present (checked in _static otherwise)
            if Handler.ui_dir and (Handler.ui_dir / "index.html").exists():
                return False  # let static handling serve the app
            self._json({"ok": True, "service": "local-control"})
            return True
        if route in ("/runtimes",) and method == "GET":
            self._json(svc.snapshot())
            return True
        if route in ("/active",) and method == "GET":
            self._json(svc.active_state())
            return True
        if ctl and route in ("/models",) and method == "GET":
            # gated on /ctl: at ROOT, GET /models is the forked UI's SPA page
            self._json(svc.models())
            return True
        if route in ("/bench",) and method == "GET":
            self._json(svc.bench_list())
            return True
        if route in ("/bench",) and method == "POST":
            data = json.loads(self._body() or b"{}")
            self._json(svc.bench_run(data.get("device", ""), data.get("model")))
            return True
        if len(parts) == 3 and parts[0] == "runtimes":
            rid, action = parts[1], parts[2]
            if action == "log" and method == "GET":
                st = svc.state[rid]  # KeyError -> 404
                tail = int(parse_qs(url.query).get("tail", ["80"])[0])
                self._json({"id": rid, "lines": list(st.lines)[-tail:]})
                return True
            if action in ("start", "stop", "activate") and method == "POST":
                if action == "start":
                    self._json(svc.start(rid))
                elif action == "stop":
                    self._json(svc.stop(rid))
                else:
                    self._json(svc.activate(rid))
                return True
        return False

    def _proxyable(self, method: str, path: str) -> bool:
        if path.startswith(PROXY_PREFIXES):
            return True
        # llama router API: non-GET on /models goes to the active server;
        # GET /models is the forked UI's models SPA page
        if path in ("/models", "/models/") and method != "GET":
            return True
        return False

    # ---- static SPA (M4 hosting) ----------------------------------------
    def _static(self, path: str) -> bool:
        ui = Handler.ui_dir
        if not ui:
            return False
        rel = path.lstrip("/") or "index.html"
        target = (ui / rel).resolve()
        try:
            target.relative_to(ui.resolve())
        except ValueError:
            self._json({"error": "forbidden"}, 403)
            return True
        if target.is_file():
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            data = target.read_bytes()
            self._headers(200, ctype, len(data))
            self.end_headers()
            self.wfile.write(data)
            return True
        # SPA fallback: extensionless paths (routes) get the app shell;
        # missing assets 404 so broken deploys stay visible
        index = ui / "index.html"
        if index.exists() and "." not in rel.rsplit("/", 1)[-1]:
            data = index.read_bytes()
            self._headers(200, "text/html", len(data))
            self.end_headers()
            self.wfile.write(data)
            return True
        return False

    # ---- API proxy to the active runtime --------------------------------
    def _proxy(self, method: str, url) -> None:
        svc = self.service
        target = svc.active_api_base()
        if not target:
            self._json({"error": "no active runtime — start one and press Activate"}, 503)
            return
        host, _, port = target.rpartition(":")
        # socket timeout = INACTIVITY cap, not total: 300s tolerates a slow
        # CPU generation between tokens while still catching a hung server;
        # SSE keeps flowing as long as tokens arrive.
        conn = http.client.HTTPConnection(host, int(port), timeout=300)
        try:
            body = self._body()
            headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in ("host", "connection", "content-length")}
            if body:
                headers["Content-Length"] = str(len(body))
            conn.request(method, url.path + (f"?{url.query}" if url.query else ""),
                         body=body or None, headers=headers)
            resp = conn.getresponse()
            # send_response + copied headers (streaming: no Content-Length)
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() in ("transfer-encoding", "connection", "content-length"):
                    continue
                self.send_header(k, v)
            if "access-control-allow-origin" not in {k.lower() for k, _ in resp.getheaders()}:
                self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            while True:  # stream chunks — SSE must reach the client live
                chunk = resp.read(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        finally:
            conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="local AI runtime control service")
    ap.add_argument("--port", type=int, default=8098)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--config", default=str(Path(__file__).with_name("runtimes.json")))
    ap.add_argument("--ui-dir", default=str(Path(__file__).resolve().parents[1] / "tools" / "ui" / "dist"),
                    help="built llama-ui SPA to serve at / (empty string disables)")
    args = ap.parse_args()

    Handler.service = Service(Path(args.config))
    Handler.ui_dir = Path(args.ui_dir) if args.ui_dir else None
    ui_state = (f"UI={args.ui_dir}" if Handler.ui_dir and (Handler.ui_dir / "index.html").exists()
                else "UI=off (no index.html)")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"local-control: {len(Handler.service.runtimes)} runtimes "
          f"on http://{args.host}:{args.port}  ({ui_state}; GET /runtimes, /ctl/*, proxy /v1/*)",
          flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
