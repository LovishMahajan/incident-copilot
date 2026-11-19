---
doc_id: pi-2026-044
version: r1
title: Past incident 2026-044 payments timeout
doc_type: past_incident
services: payments-api
allowed_groups: payments-team, sre
owner: payments-team (synthetic)
reviewed_at: 2026-08-02T09:00:00Z
deprecated: false
---
# Past incident 2026-044 payments timeout

Synthetic past incident review.

## What happened

PAYMENTS_TIMEOUT_MS was lowered from 3000 to 300 in a config change. Card authorisations timed out across all versions and checkout failed for a third of customers.

## How it was fixed

The value was restored to 3000. Follow-up: the setting gained a lower bound of 1000.
