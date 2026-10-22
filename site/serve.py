#!/usr/bin/env python3
"""Serve the MILAAN website and the local synthetic Streamlit demo."""

from __future__ import annotations

import argparse
import http.server
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

SITE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SITE_DIR.parent
DEMO_DB = PROJECT_ROOT / "demo" / "milaan_demo.db"
DEMO_SETUP = PROJECT_ROOT / "demo" / "setup_demo.py"
DEMO_URL = "http://127.0.0.1:8501"


def port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex((host, port)) == 0


def wait_for_demo(url: str, timeout: float = 25.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status < 500:
                    return True
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(0.4)
    return False


def ensure_demo_database() -> None:
    if DEMO_DB.exists():
        return
    if not DEMO_SETUP.exists():
        raise FileNotFoundError("demo/setup_demo.py is missing")
    subprocess.run([sys.executable, str(DEMO_SETUP)], cwd=PROJECT_ROOT, check=True)


class SiteHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(SITE_DIR), **kwargs)

    def do_GET(self) -> None:
        requested = urlparse(self.path).path
        if requested in {"/demo", "/demo/"}:
            self.send_response(302)
            self.send_header("Location", DEMO_URL)
            self.end_headers()
            return
        if requested == "/":
            self.path = "/index.html"
        super().do_GET()

    def log_message(self, format: str, *args) -> None:
        sys.stderr.write("site: " + (format % args) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-port", type=int, default=8000)
    parser.add_argument("--no-demo", action="store_true", help="Serve only the static site")
    args = parser.parse_args()

    if port_open("127.0.0.1", args.site_port):
        print(f"Port {args.site_port} is already in use.", file=sys.stderr)
        return 2

    demo_process = None
    if not args.no_demo:
        if port_open("127.0.0.1", 8501):
            print("Using the Streamlit demo already running on port 8501.")
        else:
            ensure_demo_database()
            env = os.environ.copy()
            env["DATABASE_PATH"] = str(DEMO_DB)
            env["PYTHONUNBUFFERED"] = "1"
            env["BROWSER_GATHER_USAGE_STATS"] = "false"
            demo_process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "streamlit",
                    "run",
                    str(PROJECT_ROOT / "app.py"),
                    "--server.headless=true",
                    "--server.address=127.0.0.1",
                    "--server.port=8501",
                    "--browser.gatherUsageStats=false",
                ],
                cwd=PROJECT_ROOT,
                env=env,
            )
            if not wait_for_demo(DEMO_URL):
                print("The Streamlit demo did not start in time.", file=sys.stderr)
                demo_process.terminate()
                return 1
            print(f"Started the synthetic Streamlit demo on {DEMO_URL}.")

    site_url = f"http://127.0.0.1:{args.site_port}"
    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.site_port), SiteHandler)
    print(f"MILAAN website: {site_url}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping MILAAN website.")
    finally:
        server.server_close()
        if demo_process is not None:
            demo_process.terminate()
            try:
                demo_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                demo_process.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
