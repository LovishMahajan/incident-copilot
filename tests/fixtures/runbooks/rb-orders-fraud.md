---
doc_id: rb-orders-fraud
version: 77e0a3b
title: Orders fraud controls
doc_type: runbook
services: orders-api
allowed_groups: security
owner: security (synthetic)
reviewed_at: 2026-10-20T09:00:00Z
deprecated: false
---
# Orders fraud controls

Synthetic. Restricted to the security group.

## Fraud rule FR-ORD-9 false positives

When FR-ORD-9 blocks legitimate orders, orders-api returns HTTP 500 on POST /v1/orders for the blocked carts. The security on-call disables the rule; nobody else changes it.

## ORDER_TAX_RATE_BPS tampering

An unexpected change to ORDER_TAX_RATE_BPS outside the release process is treated as a security incident.
