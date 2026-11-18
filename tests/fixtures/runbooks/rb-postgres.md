---
doc_id: rb-postgres
version: 1f2e3d4
title: PostgreSQL operations
doc_type: runbook
services: *
allowed_groups: orders-team, payments-team, checkout-team, inventory-team, search-team, identity-team, sre
owner: platform-team (synthetic)
reviewed_at: 2026-09-20T09:00:00Z
deprecated: false
---
# PostgreSQL operations

Synthetic. Shared database clusters.

## Connection pool exhaustion

Errors such as "remaining connection slots are reserved" mean the pool is exhausted. Check pool size settings such as ORDERS_DB_POOL_SIZE against max_connections before raising them.

## Failover db-failover-7

Procedure db-failover-7 promotes the standby when the primary is lost. Writes fail for up to a minute; clients reconnect on their own.

## Replication lag

PostgresReplicationLag fires above 30 seconds of lag. Long-running queries on the standby and bulk writes on the primary are the usual causes.
