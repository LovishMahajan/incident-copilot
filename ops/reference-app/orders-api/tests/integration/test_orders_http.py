"""The service over HTTP, with the production configuration from deploy/production.env."""

import json
import threading
import unittest
import urllib.error
import urllib.request

from orders.app import make_server


class OrdersOverHttp(unittest.TestCase):
    def setUp(self):
        self.server = make_server(0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_create_order(self):
        status, body = self.call(
            "POST", "/v1/orders", {"items": [{"sku": "a", "qty": 1, "unit_price": "100.00"}]}
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["total"], "108.25")

    def test_bad_request(self):
        status, _ = self.call("POST", "/v1/orders", {"no_items": True})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
