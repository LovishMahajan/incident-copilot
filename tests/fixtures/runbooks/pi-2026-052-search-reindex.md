---
doc_id: pi-2026-052
version: r1
title: Past incident 2026-052 empty search results
doc_type: past_incident
services: search-api
allowed_groups: search-team, sre
owner: search-team (synthetic)
reviewed_at: 2026-09-01T09:00:00Z
deprecated: false
---
# Past incident 2026-052 empty search results

Synthetic past incident review.

## What happened

A nightly reindex swapped the products alias to an index that had not finished building. Searches returned no products for 25 minutes.

## How it was fixed

The alias was swapped back. Follow-up: the reindex job checks the document count before the swap.
