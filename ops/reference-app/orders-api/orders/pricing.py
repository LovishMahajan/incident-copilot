"""Order totals. The tax rate is in basis points: 825 means 8.25%."""

from decimal import ROUND_HALF_UP, Decimal


def subtotal(items):
    return sum((Decimal(str(i["unit_price"])) * int(i["qty"]) for i in items), Decimal("0"))


def compute_totals(cart, settings):
    """Read on every order, so a bad value fails here and not at start-up."""
    digits = Decimal(1).scaleb(-int(settings.order_rounding_digits))
    net = subtotal(cart)
    tax_rate_bps = int(settings.order_tax_rate_bps)
    tax = (net * tax_rate_bps / Decimal(10000)).quantize(digits, rounding=ROUND_HALF_UP)
    return {
        "currency": settings.order_currency,
        "subtotal": str(net.quantize(digits)),
        "tax": str(tax),
        "total": str((net + tax).quantize(digits)),
    }
