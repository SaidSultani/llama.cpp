"""Tests for local-control/control.py.

Uses a FAKE runtime (/bin/sh) and a throwaway config — no real inference
process is started, so the suite is fast and side-effect free. The service
is exercised the way a client would: spawned as a subprocess, driven over
HTTP, then torn down.
"""
import json
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

PORT = 8099
BASE = f"http://127.0.0.1:{PORT}"


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
        cls.tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"runtimes": [{
            "id": "fake",
            "label": "fake sleeper with output",
            "kind": "test",
            "platform": "linux",
            "args": ["/bin/sh", "-c", "echo boot-line; sleep 60"],
            "health_port": None,
        }]}, cls.tmp)
        cls.tmp.close()
        cls.proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("control.py")),
             "--port", str(PORT), "--config", cls.tmp.name],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                code, _ = http("GET", "/health")
                if code == 200:
                    return
            except (urllib.error.URLError, ConnectionError, OSError):
                time.sleep(0.2)
        raise RuntimeError("service did not come up: " + ((cls.proc.stdout.read() if cls.proc.stdout else "") or ""))

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=5)
        Path(cls.tmp.name).unlink(missing_ok=True)

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
        # second start must refuse — never silently double-bind
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
        _, snap = http("GET", "/runtimes")
        entry = next(r for r in snap["runtimes"] if r["id"] == "fake")
        self.assertEqual(entry["status"], "exited")


if __name__ == "__main__":
    unittest.main(verbosity=2)
