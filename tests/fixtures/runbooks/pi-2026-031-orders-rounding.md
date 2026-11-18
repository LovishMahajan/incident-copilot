---
doc_id: pi-2026-031
version: r2
title: Past incident 2026-031 orders rounding setting
doc_type: past_incident
services: orders-api
allowed_groups: orders-team, sre
owner: orders-team (synthetic)
reviewed_at: 2026-07-14T09:00:00Z
deprecated: false
---
# Past incident 2026-031 orders rounding setting

Synthetic past incident review.

## What happened

Release 2.9.0 set ORDER_ROUNDING_DIGITS to "two" instead of 2. Every POST /v1/orders failed with ValueError in compute_totals and returned HTTP 500 on the new instances only.

## How it was fixed

The config was reverted to the previous revision within twelve minutes. Follow-up: settings that the order path parses as integers are validated in the release pipeline.
