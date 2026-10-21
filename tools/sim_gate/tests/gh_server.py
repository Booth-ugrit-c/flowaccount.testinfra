from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class Recorded:
    def __init__(self, path, headers):
        self.path = path
        self.headers = {k.lower(): v for k, v in headers.items()}


class FakeGitHub:
    def __init__(self):
        self.routes = {}
        self.requests = []
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parent.requests.append(Recorded(self.path, self.headers))
                route = parent.routes.get(self.path)
                if route is None:
                    route = parent.routes.get(self.path.split("?", 1)[0], (404, b"{}", {}))
                status, body, headers = route
                if callable(body):
                    body = body()
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def serve(self, path, body, status=200, headers=None):
        self.routes[path] = (status, body, headers or {})

    def paths(self):
        return [r.path for r in self.requests]
