---
doc_id: rb-deploys
version: 6a7b8c9
title: Releases and rollbacks
doc_type: runbook
services: *
allowed_groups: orders-team, payments-team, checkout-team, inventory-team, search-team, identity-team, sre
owner: platform-team (synthetic)
reviewed_at: 2026-10-15T09:00:00Z
deprecated: false
---
# Releases and rollbacks

Synthetic. Applies to every service released through the gradual rollout.

## Gradual rollout paused

The rollout moves traffic to new instances at 5%, 25%, 50% and 100%. Pause it with deployctl rollout pause <service> as soon as errors grow with each step; paused instances keep the share they had.

## Rolling back a release

deployctl rollback <service> --to <previous version> returns every instance to the previous version. It needs an approver and takes about five minutes.

## Reading error rate by version

The error_rate_by_version panel splits the error rate by the service.version label. A failing new version beside a healthy old one is the strongest sign a release caused the incident.
