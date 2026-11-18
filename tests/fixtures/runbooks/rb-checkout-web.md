---
doc_id: rb-checkout-web
version: 91aa0c4
title: Checkout web runbook
doc_type: runbook
services: checkout-web
allowed_groups: checkout-team, sre
owner: checkout-team (synthetic)
reviewed_at: 2026-10-10T09:00:00Z
deprecated: false
---
# Checkout web runbook

Synthetic. checkout-web renders the cart and checkout pages.

## Cart page latency

CheckoutCartLatencyHigh fires when the cart page p95 goes above 800 ms. The usual cause is a slow call to inventory-svc for stock levels; check the inventory dependency first.

## Feature flag rollback

A bad feature flag is switched off with flagctl off checkout.<flag-name>. Flags such as checkout.new_address_form roll out by percentage, so errors that grow with the flag's share point at the flag.

## Session cookie errors

Customers logged out at checkout with SESSION_COOKIE_INVALID in the logs usually means the cookie signing secret was rotated without a grace period. Ask identity-team to restore the previous key.
