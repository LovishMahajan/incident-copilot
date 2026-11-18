---
doc_id: rb-orders-api-legacy
version: 0b77d10
title: Orders API runbook (old)
doc_type: runbook
services: orders-api
allowed_groups: orders-team, sre
owner: orders-team (synthetic)
reviewed_at: 2025-11-02T09:00:00Z
deprecated: true
---
# Orders API runbook (old)

Synthetic and deprecated. Kept to show that stale documents are not searched.

## HTTP 500 after deploy

Restart every orders-api instance with orderctl restart --all, then wait. ORDER_TAX_RATE_BPS errors usually clear after a restart.
