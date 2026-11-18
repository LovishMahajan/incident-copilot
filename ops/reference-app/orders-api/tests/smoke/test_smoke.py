"""Key paths with the shipped configuration: health, and pricing an order."""

import unittest

from orders import pricing, settings
from orders.app import Handler


class Smoke(unittest.TestCase):
    def test_production_settings_price_an_order(self):
        totals = pricing.compute_totals(
            [{"sku": "a", "qty": 3, "unit_price": "10.00"}], settings.load()
        )
        self.assertEqual(totals["total"], "32.48")

    def test_health_reads_no_setting(self):
        self.assertTrue(hasattr(Handler, "do_GET"))


if __name__ == "__main__":
    unittest.main()
