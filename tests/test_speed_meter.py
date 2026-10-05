import contextlib
import os
import re
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from speed_meter import Measurement, summarize

ROOT = Path(__file__).resolve().parents[1]


@contextlib.contextmanager
def serve():
    state = {"count": 0, "active": 0, "max_active": 0, "sizes": [], "headers": []}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            with lock:
                state["count"] += 1
                number = state["count"]
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
                state["headers"].append(dict(self.headers))
            try:
                if self.path == "/error":
                    self.send_error(404)
                    return
                if self.path == "/bad-chunked":
                    self.send_response(200)
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    self.wfile.write(b"10\r\nshort")
                    self.close_connection = True
                    return
                if self.path == "/stall":
                    time.sleep(0.2)
                body = b"x" * (70_000 + number * 100)
                if self.path == "/empty":
                    body = b""
                self.send_response(200)
                if self.path != "/no-length":
                    length = len(body) + (1_000 if self.path == "/truncated" else 0)
                    self.send_header("Content-Length", str(length))
                else:
                    self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self.close_connection = self.path in ("/empty", "/truncated", "/stall")
                def write_chunk(data):
                    if self.path == "/no-length":
                        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                    else:
                        self.wfile.write(data)
                    self.wfile.flush()
                time.sleep(0.01)
                if body:
                    write_chunk(body[:100])
                time.sleep(0.01)
                for offset in range(100, len(body), 4096):
                    write_chunk(body[offset:offset + 4096])
                    time.sleep(0.001)
                if self.path == "/no-length":
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                with lock:
                    state["sizes"].append(len(body))
                    # Mark completion before EOF becomes visible to the next request.
                    state["active"] -= 1
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                with lock:
                    state["active"] -= 1
            finally:
                if self.path in ("/error", "/bad-chunked"):
                    with lock:
                        state["active"] -= 1

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def run_cli(*args):
    return subprocess.run(
        [sys.executable, str(ROOT / "speed_meter.py"), *args],
        capture_output=True, text=True, timeout=15, cwd=ROOT,
        env=dict(os.environ, no_proxy="127.0.0.1", NO_PROXY="127.0.0.1"),
    )


class FormulaTests(unittest.TestCase):
    def test_speed_uses_total_bytes_and_total_time(self):
        result = summarize([Measurement(1, 1_000_000), Measurement(3, 9_000_000)])
        self.assertEqual(result.average_time, 2)
        self.assertEqual(result.total_bytes, 10_000_000)
        self.assertEqual(result.megabytes_per_second, 2.5)


class CliTests(unittest.TestCase):
    def test_ten_complete_sequential_downloads_and_summary(self):
        with serve() as (url, state):
            result = run_cli(url + "/file")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(state["count"], 10)
            self.assertEqual(state["max_active"], 1)
            self.assertEqual(state["sizes"], [70_000 + n * 100 for n in range(1, 11)])
            self.assertTrue(all(h["Accept-Encoding"] == "identity" for h in state["headers"]))
            self.assertTrue(all(h["Cache-Control"] == "no-cache" for h in state["headers"]))
        rows = re.findall(r"Request (\d+)/10: ([\d.]+) s, (\d+) bytes", result.stdout)
        self.assertEqual([int(row[0]) for row in rows], list(range(1, 11)))
        self.assertEqual([int(row[2]) for row in rows], state["sizes"])
        times = [float(row[1]) for row in rows]
        self.assertTrue(all(t >= 0.02 for t in times), "Timer must include body transfer")
        average = float(re.search(r"Average request time: ([\d.]+)", result.stdout)[1])
        mbps = float(re.search(r"Download speed: ([\d.]+) MB/s", result.stdout)[1])
        mbitps = float(re.search(r"Download speed: ([\d.]+) Mbit/s", result.stdout)[1])
        self.assertIn("Downloaded: 705500 bytes (0.705500 MB)", result.stdout)
        self.assertAlmostEqual(average, sum(times) / 10, delta=0.000002)
        self.assertAlmostEqual(mbps, 0.7055 / sum(times), delta=0.001)
        self.assertAlmostEqual(mbitps, mbps * 8, delta=0.00001)

    def test_counts_actual_bytes_without_content_length(self):
        with serve() as (url, state):
            result = run_cli(url + "/no-length")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(state["count"], 10)
            self.assertIn("Downloaded: 705500 bytes", result.stdout)

    def test_http_error_empty_and_incomplete_body_stop_without_summary(self):
        for path in ("/error", "/empty", "/truncated", "/bad-chunked"):
            with self.subTest(path=path), serve() as (url, state):
                result = run_cli(url + path)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(state["count"], 1)
                self.assertIn("Request 1/10 failed:", result.stderr)
                self.assertNotIn("Download speed:", result.stdout)

    def test_network_timeout_stops_without_summary(self):
        with serve() as (url, state):
            result = run_cli(url + "/stall", "--timeout", "0.05")
            self.assertEqual(result.returncode, 1)
            self.assertEqual(state["count"], 1)
            self.assertIn("timed out", result.stderr)
            self.assertNotIn("Download speed:", result.stdout)

    def test_invalid_arguments_fail_before_network(self):
        with serve() as (url, state):
            cases = [[], ["file:///tmp/image"], ["example.com"], ["http://localhost:bad"]]
            cases.extend([url, "--timeout", value] for value in ("0", "-1", "nan", "inf", "abc"))
            for args in cases:
                with self.subTest(args=args):
                    result = run_cli(*args)
                    self.assertEqual(result.returncode, 2)
                    self.assertNotIn("Download speed:", result.stdout)
            self.assertEqual(state["count"], 0)


if __name__ == "__main__":
    unittest.main()
