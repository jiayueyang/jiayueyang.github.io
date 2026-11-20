"""Serve the standalone public reader locally. Start FastAPI for a private workspace."""
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", default=3000, type=int)
parser.add_argument("--strictPort", action="store_true")
args = parser.parse_args()
web = Path(__file__).resolve().parents[1] / "web"
handler = partial(SimpleHTTPRequestHandler, directory=str(web))
ThreadingHTTPServer((args.host, args.port), handler).serve_forever()
