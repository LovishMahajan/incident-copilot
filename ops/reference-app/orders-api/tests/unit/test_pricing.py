import unittest
from decimal import Decimal

from orders import pricing
from orders.settings import Settings


def config(bps):
    return Settings("USD", "50", "2", bps)


CART = [{"sku": "sku-1", "qty": 2, "unit_price": "50.00"}]


class ComputeTotals(unittest.TestCase):
    def test_tax_in_basis_points(self):
        totals = pricing.compute_totals(CART, config("825"))
        self.assertEqual(totals["tax"], "8.25")
        self.assertEqual(totals["total"], "108.25")

    def test_subtotal(self):
        self.assertEqual(pricing.subtotal(CART), Decimal("100.00"))

    def test_rounding(self):
        totals = pricing.compute_totals([{"qty": 1, "unit_price": "0.10"}], config("825"))
        self.assertEqual(totals["tax"], "0.01")


if __name__ == "__main__":
    unittest.main()
