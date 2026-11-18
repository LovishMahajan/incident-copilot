"""Settings from an env file: KEY=VALUE lines, `#` comments. Values stay strings until used."""

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ENV_FILE = Path(__file__).resolve().parent.parent / "deploy" / "production.env"


@dataclass(frozen=True)
class Settings:
    order_currency: str
    order_max_items: str
    order_rounding_digits: str
    order_tax_rate_bps: str


def read_env_file(path):
    values = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def load(path=None):
    values = read_env_file(path or os.environ.get("ORDERS_ENV_FILE") or DEFAULT_ENV_FILE)
    return Settings(
        order_currency=values["ORDER_CURRENCY"],
        order_max_items=values["ORDER_MAX_ITEMS"],
        order_rounding_digits=values["ORDER_ROUNDING_DIGITS"],
        order_tax_rate_bps=values["ORDER_TAX_RATE_BPS"],
    )
