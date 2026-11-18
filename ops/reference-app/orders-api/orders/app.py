"""orders-api: POST /v1/orders and GET /healthz, on the standard library's HTTP server.

`python -m orders.app` serves on 127.0.0.1:$PORT (default 8080) with the settings in
$ORDERS_ENV_FILE (default deploy/production.env). An unhandled error is logged to standard
error with its traceback and answered with HTTP 500.
"""

import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from orders import pricing, settings


class Handler(BaseHTTPRequestHandler):
    env_file = None  # set by make_server

    def log_message(self, fmt, *args):
        sys.stderr.write("access " + (fmt % args) + "\n")

    def _reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/healthz":  # liveness only: reads no setting
            return self._reply(200, {"status": "ok"})
        return self._reply(404, {"error": "not_found"})

    def do_POST(self):
        if self.path != "/v1/orders":
            return self._reply(404, {"error": "not_found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            order = json.loads(self.rfile.read(length) or b"{}")
            current = settings.load(self.env_file)
            if len(order["items"]) > int(current.order_max_items):
                return self._reply(422, {"error": "too_many_items"})
            totals = pricing.compute_totals(order["items"], current)
        except (KeyError, TypeError, json.JSONDecodeError):
            return self._reply(400, {"error": "bad_request"})
        except Exception:
            sys.stderr.write("unhandled exception in create_order\n" + traceback.format_exc())
            return self._reply(500, {"error": "internal"})
        return self._reply(201, {"status": "created", **totals})


def make_server(port=0, env_file=None):
    handler = type("BoundHandler", (Handler,), {"env_file": env_file})
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def main():
    server = make_server(int(os.environ.get("PORT", "8080")))
    sys.stderr.write(f"orders-api listening on {server.server_address[1]}\n")
    sys.stderr.flush()
    server.serve_forever()


if __name__ == "__main__":
    main()
