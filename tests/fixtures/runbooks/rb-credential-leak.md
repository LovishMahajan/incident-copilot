---
doc_id: rb-credential-leak
version: 3e4f5a6
title: Leaked credential procedure
doc_type: runbook
services: *
allowed_groups: security
owner: security (synthetic)
reviewed_at: 2026-10-18T09:00:00Z
deprecated: false
---
# Leaked credential procedure

Synthetic. Restricted to the security group.

## Rotate first

When a credential appears in a log, a ticket or model output, rotate it before investigating how it leaked. Then mask the stored copy and trace it to the evidence that held it.
