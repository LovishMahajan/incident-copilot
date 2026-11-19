---
doc_id: rb-search-api
version: 2b3c4d5
title: Search API runbook
doc_type: runbook
services: search-api
allowed_groups: search-team, sre
owner: search-team (synthetic)
reviewed_at: 2026-10-05T09:00:00Z
deprecated: false
---
# Search API runbook

Synthetic. search-api serves product search from an Elasticsearch cluster.

## Elasticsearch cluster red

A red cluster means a primary shard is unassigned. Find it with the allocation explain API, then fix the node or restore the shard from snapshot.

## Query timeouts

SearchQueryTimeouts fires when more than 1% of queries pass SEARCH_QUERY_TIMEOUT_MS. Heavy wildcard queries and a cold cache after a reindex are the usual causes.

## Reindex alias swap

Reindexing writes to a new index and then swaps the products alias. If results disappear after a reindex, the alias points at an empty index: swap it back to the previous one.
