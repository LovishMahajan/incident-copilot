---
doc_id: rb-payments-api
version: c3d4e5f
title: Payments API runbook
doc_type: runbook
services: payments-api
allowed_groups: payments-team, sre
owner: payments-team (synthetic)
reviewed_at: 2026-09-15T09:00:00Z
deprecated: false
---
# Payments API runbook

Synthetic. payments-api authorises cards for every service that takes money.

## HTTP 500 after deploy

If PaymentsApiHighErrorRate fires after a release, compare the error rate by version. Server errors only on the new version mean the release is at fault: roll it back with the release process.

## PAYMENTS_TIMEOUT_MS tuning

PAYMENTS_TIMEOUT_MS is the upstream card network timeout. Values below 1000 cause a spike of TimeoutError on authorise calls. The supported range is 2000 to 5000.

## Card authorisation declines spike

A sudden rise in declined authorisations with reason code 05 usually comes from the card network, not from us. Check the network status feed and the decline rate per issuer before paging.

## Webhook backlog

When the PaymentsWebhookBacklog alert fires, the settlement webhook queue is growing faster than consumers drain it. Scale the payments-webhook consumer group and check for poison messages.
