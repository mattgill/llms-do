#!/usr/bin/env python3
"""Serve the generated dashboard from public/ using only the standard library."""

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PUBLIC_DIR = ROOT / "public"


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the generated LLMs Do dashboard")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="address to bind to (use 0.0.0.0 for remote access; default: 127.0.0.1)",
    )
    parser.add_argument("--port", type=int, default=8000, help="port to listen on (default: 8000)")
    args = parser.parse_args()

    if not PUBLIC_DIR.is_dir():
        parser.error(f"public directory does not exist: {PUBLIC_DIR}")

    handler = partial(SimpleHTTPRequestHandler, directory=str(PUBLIC_DIR))
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Serving {PUBLIC_DIR}")
    print(f"Open http://{args.host}:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
