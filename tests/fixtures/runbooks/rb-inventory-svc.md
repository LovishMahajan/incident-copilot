---
doc_id: rb-inventory-svc
version: 5e6f7a8
title: Inventory service runbook
doc_type: runbook
services: inventory-svc
allowed_groups: inventory-team, sre
owner: inventory-team (synthetic)
reviewed_at: 2026-08-30T09:00:00Z
deprecated: false
---
# Inventory service runbook

Synthetic. inventory-svc reserves stock for orders.

## Stock reservation deadlocks

ERROR 40P01 deadlock detected on the stock_reservations table happens when two orders reserve the same items in a different order. Retries hide a few; a burst means a release changed the locking order.

## Kafka consumer lag

InventoryConsumerLag fires when the stock-events consumer is more than 10,000 messages behind. Check partition skew and restart stuck consumers one at a time.

## Reservation TTL

RESERVATION_TTL_SECONDS controls how long held stock stays reserved. Too short and carts lose items at payment; too long and stock looks sold out.
