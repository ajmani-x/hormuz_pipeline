"""
Local Live Dashboard
---------------------
A minimal stdlib-only HTTP server exposing the pipeline as a JSON API
(/api/run), plus the static dashboard (dashboard.html) that polls it on an
interval, renders the report, and can read a summary aloud via the browser's
Web Speech API.

    python server.py
    python server.py --port 9000

No new dependencies — only http.server / urllib from the standard library,
consistent with the rest of this project.
"""
import argparse
import json
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from models import to_dict
from orchestrator import run_pipeline

DASHBOARD_HTML_PATH = Path(__file__).with_name("dashboard.html")


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "HormuzDashboard/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[server] {self.address_string()} - {fmt % args}\n")

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self._send_dashboard()
        elif parsed.path == "/api/run":
            self._send_run(urllib.parse.parse_qs(parsed.query))
        else:
            self._send_json(404, {"error": "not found"})

    def _send_dashboard(self):
        try:
            body = DASHBOARD_HTML_PATH.read_bytes()
        except FileNotFoundError:
            self._send_json(500, {"error": "dashboard.html missing"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_run(self, query: dict):
        def first(name, default, cast):
            if query.get(name) and query[name][0] != "":
                try:
                    return cast(query[name][0])
                except (ValueError, TypeError):
                    return default
            return default

        scenario = first("scenario", "Strait of Hormuz flow disruption", str)
        disruption = first("disruption", 0.5, float)
        region = first("region", "Hormuz", str)
        seed = first("seed", None, int)  # unset -> fresh randomness each call, for a "live" feel

        try:
            rec = run_pipeline(
                scenario_name=scenario,
                disruption_pct=disruption,
                region_of_concern=region,
                seed=seed,
            )
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})
            return

        self._send_json(200, to_dict(rec))


def main():
    parser = argparse.ArgumentParser(description="Local live dashboard for the Hormuz pipeline")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    url = f"http://{args.host}:{args.port}/"
    print(f"Hormuz dashboard running at {url} (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
