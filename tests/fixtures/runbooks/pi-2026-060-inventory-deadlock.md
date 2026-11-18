---
doc_id: pi-2026-060
version: r1
title: Past incident 2026-060 stock reservation deadlocks
doc_type: past_incident
services: inventory-svc
allowed_groups: inventory-team, sre
owner: inventory-team (synthetic)
reviewed_at: 2026-09-25T09:00:00Z
deprecated: false
---
# Past incident 2026-060 stock reservation deadlocks

Synthetic past incident review.

## What happened

Release 5.2.0 reserved items in cart order instead of by SKU. Concurrent orders deadlocked with ERROR 40P01 and order placement slowed to a crawl.

## How it was fixed

The release was rolled back. Follow-up: reservations always lock items in SKU order.
