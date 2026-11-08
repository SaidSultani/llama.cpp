"""Tests for local-control/control.py.

Fake runtimes only — no real inference process and no bench run. Covers:
start/stop lifecycle, /ctl aliases, SPA hosting, model store, bench listing,
and the API proxy against an in-test backend server. The service is
exercised the way a client would: spawned as a subprocess, driven over
HTTP, torn down.
"""
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

PORT = 8099
BASE = f"http://127.0.0.1:{PORT}"
BACKEND_PORT = 8177
BACKEND_PAYLOAD = {"data": [{"id": "fake-backend-model", "object": "model"}]}


class _Backend(BaseHTTPRequestHandler):
    """Stand-in llama-server the proxy can target."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            body = json.dumps(BACKEND_PAYLOAD).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


def http(method: str, path: str):
    req = urllib.request.Request(BASE + path, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:  # 4xx/5xx still carry our JSON body
        return exc.code, json.loads(exc.read())


class ControlServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="hermes-verify-control-"))
        cls.backend = HTTPServer(("127.0.0.1", BACKEND_PORT), _Backend)
        threading.Thread(target=cls.backend.serve_forever, daemon=True).start()

        ui = cls.tmpdir / "ui"
        ui.mkdir()
        (ui / "index.html").write_text("<html><body>LOCAL-UI-FIXTURE</body></html>")

        md = cls.tmpdir / "models"
        md.mkdir()
        (md / "tiny-q4.gguf").write_bytes(b"x" * 4096)
        (md / "ggml-vocab-qwen3.gguf").write_bytes(b"y" * 1024)

        bd = cls.tmpdir / "bench-results"
        bd.mkdir()
        (bd / "baseline-npu.json").write_text(json.dumps({
            "device": "npu", "device_id": "HTP0", "model_id": "unit/test:Q4_K_M",
            "agg": {"ttft_ms": {"median": 225.3}, "prefill_tps": {"median": 2275.4},
                    "decode_tps": {"median": 31.1}},
        }))

        config = {
            "_comment": "unit fixture",
            "model_dirs": [str(md)],
            "bench": {"results_dir": "bench-results", "devices": ["cpu", "npu"],
                      "exe": "", "default_model": ""},
            "runtimes": [
                {"id": "fake", "label": "fake sleeper", "kind": "test",
                 "platform": "linux", "args": ["/bin/sh", "-c", "echo boot-line; sleep 60"],
                 "health_port": None},
                {"id": "fakeapi", "label": "pre-started backend", "kind": "llama",
                 "platform": "linux", "args": ["/bin/sh", "-c", "sleep 300"],
                 "health_host": "127.0.0.1", "health_port": BACKEND_PORT},
            ],
        }
        cfg = cls.tmpdir / "runtimes.json"
        cfg.write_text(json.dumps(config))

        cls.proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("control.py")),
             "--port", str(PORT), "--config", str(cfg), "--ui-dir", str(ui)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                code, _ = http("GET", "/health")
                if code == 200:
                    return
            except (urllib.error.URLError, ConnectionError, OSError):
                time.sleep(0.2)
        raise RuntimeError("service did not come up")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=5)
        cls.backend.shutdown()
        for p in sorted(cls.tmpdir.rglob("*"), reverse=True):
            p.unlink(missing_ok=True) if p.is_file() else p.rmdir()
        cls.tmpdir.rmdir()

    def test_01_unknown_runtime_is_404(self):
        code, body = http("POST", "/runtimes/nope/start")
        self.assertEqual(code, 404)
        self.assertIn("error", body)

    def test_02_initial_status_stopped(self):
        code, body = http("GET", "/runtimes")
        self.assertEqual(code, 200)
        self.assertEqual(body["runtimes"][0]["status"], "stopped")
        self.assertEqual(body["runtimes"][0]["id"], "fake")

    def test_03_start_then_double_start_conflict(self):
        code, body = http("POST", "/runtimes/fake/start")
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        code, body = http("POST", "/runtimes/fake/start")
        self.assertEqual(code, 409)
        self.assertIn("already running", body["error"])

    def test_04_running_status_and_log_lines(self):
        deadline = time.time() + 5
        lines = []
        while time.time() < deadline:
            _, body = http("GET", "/runtimes/fake/log?tail=50")
            lines = body["lines"]
            if any("boot-line" in ln for ln in lines):
                break
            time.sleep(0.2)
        self.assertTrue(any("boot-line" in ln for ln in lines), lines)
        _, snap = http("GET", "/runtimes")
        entry = next(r for r in snap["runtimes"] if r["id"] == "fake")
        self.assertEqual(entry["status"], "running")
        self.assertIsNotNone(entry["pid"])

    def test_05_stop_then_stop_again_conflict(self):
        code, body = http("POST", "/runtimes/fake/stop")
        self.assertEqual(code, 200)
        self.assertIn("exit_code", body)
        code, body = http("POST", "/runtimes/fake/stop")
        self.assertEqual(code, 409)
        self.assertIn("not running", body["error"])

    def test_06_ctl_alias_matches_root_route(self):
        code_a, a = http("GET", "/ctl/runtimes")
        code_b, b = http("GET", "/runtimes")
        self.assertEqual((code_a, code_b), (200, 200))
        self.assertEqual([r["id"] for r in a["runtimes"]], [r["id"] for r in b["runtimes"]])

    def test_07_spa_serves_index_for_routes(self):
        with urllib.request.urlopen(BASE + "/", timeout=5) as r:
            self.assertIn("LOCAL-UI-FIXTURE", r.read().decode())
            self.assertIn("text/html", r.headers.get("Content-Type", ""))
        with urllib.request.urlopen(BASE + "/control", timeout=5) as r:
            self.assertIn("LOCAL-UI-FIXTURE", r.read().decode())
        # missing extensioned asset must 404, not silently serve the shell
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(BASE + "/_app/missing.js", timeout=5)
        self.assertEqual(ctx.exception.code, 404)
        # health stays JSON next to the SPA
        code, body = http("GET", "/health")
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])

    def test_08_model_store_filters_vocab(self):
        code, body = http("GET", "/ctl/models")
        self.assertEqual(code, 200)
        names = [m["file"] for m in body["models"]]
        self.assertIn("tiny-q4.gguf", names)
        self.assertNotIn("ggml-vocab-qwen3.gguf", names)
        self.assertTrue(all(m["ram_fit_pct"] is not None for m in body["models"]), body)
        self.assertGreater(body["mem_available_mb"], 0)

    def test_09_bench_listing_parses_baseline(self):
        code, body = http("GET", "/ctl/bench")
        self.assertEqual(code, 200)
        self.assertEqual(body["devices"], ["cpu", "npu"])
        row = next(r for r in body["results"] if r["file"] == "baseline-npu.json")
        self.assertEqual(row["device"], "npu")
        self.assertEqual(row["prefill_tps"], 2275.4)

    def test_10_proxy_to_active_runtime(self):
        # not reachable -> 503 with a helpful error
        code, body = http("GET", "/v1/models")
        self.assertEqual(code, 503)
        self.assertIn("no active runtime", body["error"])
        # activate the pre-started backend (probe passes), then proxy
        code, body = http("POST", "/runtimes/fakeapi/activate")
        self.assertEqual(code, 200)
        self.assertEqual(body["active"], "fakeapi")
        with urllib.request.urlopen(BASE + "/v1/models", timeout=5) as r:
            self.assertEqual(json.loads(r.read()), BACKEND_PAYLOAD)
        code, body = http("GET", "/ctl/active")
        self.assertEqual(code, 200)
        self.assertEqual(body["id"], "fakeapi")
        self.assertTrue(body["health"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
