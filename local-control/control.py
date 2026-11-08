#!/usr/bin/env python3
"""Local-AI control service — llama.cpp fork, Phase 1 (ours).

One HTTP API to start/stop the local inference runtimes (llama.cpp builds and
GenieX serve) and read their logs, so the forked llama-ui can offer a single
runtime/device selector. Runs in WSL; Windows executables are launched through
the normal WSL interop layer (WSL executes *.exe paths natively).

Deliberate MVP choices:
- stdlib only (http.server + subprocess): no deps to break, cheap to audit.
- Process liveness is the authoritative health signal; the TCP probe is a
  best-effort extra because Windows-hosted servers are not reachable at
  127.0.0.1 from WSL2 (separate loopbacks). health_host "auto" resolves the
  Windows host via the default gateway and requires the runtime to bind
  0.0.0.0 in its args.
- One process per runtime id; starting twice is refused (409), never silently
  replaced, so ports cannot be double-bound by surprise.

Usage: python3 control.py [--port 8090] [--config runtimes.json]
"""
from __future__ import annotations

import argparse
import json
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


class Service:
    def __init__(self, config_path: Path) -> None:
        cfg = json.loads(config_path.read_text())
        self.runtimes: dict[str, dict] = {rt["id"]: rt for rt in cfg["runtimes"]}
        self.state: dict[str, RuntimeState] = {rid: RuntimeState() for rid in self.runtimes}

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
        return {"ok": True, "exit_code": st.proc.returncode}

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
                "notes": rt.get("notes"),
            })
        return {"runtimes": out, "time": time.time()}


class Handler(BaseHTTPRequestHandler):
    service: Service  # set in main()

    def _json(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload, indent=1).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")  # llama-ui may be on another port
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # quiet stdout noise
        pass

    def do_GET(self) -> None:
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        try:
            if url.path in ("/", "/health"):
                self._json({"ok": True, "service": "local-control"})
            elif parts[:1] == ["runtimes"] and len(parts) == 1:
                self._json(self.service.snapshot())
            elif len(parts) == 3 and parts[0] == "runtimes" and parts[2] == "log":
                st = self.service.state[parts[1]]  # KeyError -> 404
                tail = int(parse_qs(url.query).get("tail", ["80"])[0])
                self._json({"id": parts[1], "lines": list(st.lines)[-tail:]})
            else:
                self._json({"error": "not found"}, 404)
        except KeyError:
            self._json({"error": "unknown runtime"}, 404)

    def do_POST(self) -> None:
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        try:
            if len(parts) == 3 and parts[0] == "runtimes":
                rid, action = parts[1], parts[2]
                if action == "start":
                    self._json(self.service.start(rid))
                elif action == "stop":
                    self._json(self.service.stop(rid))
                else:
                    self._json({"error": "unknown action"}, 404)
            else:
                self._json({"error": "not found"}, 404)
        except KeyError:
            self._json({"error": "unknown runtime"}, 404)
        except RuntimeError as exc:
            # "already running" / "port busy" / "not running" — client mistakes,
            # reported with 409 so callers can distinguish from transport errors
            self._json({"error": str(exc)}, 409)


def main() -> None:
    ap = argparse.ArgumentParser(description="local AI runtime control service")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--config", default=str(Path(__file__).with_name("runtimes.json")))
    args = ap.parse_args()

    Handler.service = Service(Path(args.config))
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"local-control: {len(Handler.service.runtimes)} runtimes "
          f"on http://{args.host}:{args.port}  (GET /runtimes)", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
