---
doc_id: rb-orders-api
version: 4a1c9e2
title: Orders API runbook
doc_type: runbook
services: orders-api
allowed_groups: orders-team, sre
owner: orders-team (synthetic)
reviewed_at: 2026-10-01T09:00:00Z
deprecated: false
---
# Orders API runbook

Synthetic. The orders-api service takes and reads customer orders behind one load balancer. Alerts page the orders on-call engineer.

## HTTP 500 after deploy

When OrdersApiHighErrorRate fires within an hour of a release, compare the HTTP 500 rate of the new version with the old one before anything else. If only instances on the new version return server errors, the release is the likely cause: pause the rollout and keep the old instances serving traffic.

## Config revert

A bad configuration value is reverted to the previous config revision, not edited by hand. The release process keeps every revision, for example cfg-41 before cfg-42.

```
deployctl config revert orders-api --to <previous revision>
```

Hold for ten minutes after the revert and confirm the 500 rate is back at baseline.

## ValueError in compute_totals

orders/pricing.py compute_totals reads ORDER_TAX_RATE_BPS on every POST /v1/orders and parses it with int(). The value is basis points: 825 means 8.25%. A decimal value such as 8.25 raises ValueError and the order request fails with HTTP 500. Health checks do not read the setting, so new instances still pass their gate.

## Payment gateway timeouts

TimeoutError: payment gateway did not answer within 3000 ms is a known signature at about 1 in 2,000 requests. It is not a release problem unless its rate rises. Check the payment-gateway status page and PAYMENTS_TIMEOUT_MS before escalating.

## orders-db connection resets

A single "orders-db connection reset; reconnected on retry" warning is normal pool churn. Repeated resets across instances point at the database: see the PostgreSQL operations runbook.
