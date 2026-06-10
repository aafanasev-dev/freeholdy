"""Tiny echo HTTP server — stdlib only, no dependencies.

Echoes the request body back verbatim with 200 for both GET and POST. A GET with
no body falls back to echoing the request path (minus the leading slash) so a bare
`curl https://host/foo` still returns something deterministic ("foo").

Used by tests/test_nginx.sh to prove the nginx -> container proxy path carries
traffic intact end to end.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8080


class EchoHandler(BaseHTTPRequestHandler):
    def _echo(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else self.path.lstrip("/").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _echo
    do_POST = _echo

    def log_message(self, *args):  # keep container logs quiet
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), EchoHandler).serve_forever()
