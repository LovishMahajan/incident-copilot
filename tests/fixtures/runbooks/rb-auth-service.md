---
doc_id: rb-auth-service
version: 8c9d0e1
title: Auth service runbook
doc_type: runbook
services: auth-service
allowed_groups: identity-team, security
owner: identity-team (synthetic)
reviewed_at: 2026-10-12T09:00:00Z
deprecated: false
---
# Auth service runbook

Synthetic. auth-service issues and verifies sign-in tokens.

## JWT signing key rotation

Keys rotate every 30 days. Publish the new key in the JWKS document a day before signing with it, so verifiers have it cached when the first token arrives.

## Login 401 spike

A spike of HTTP 401 on /oauth/token after a deploy usually means the audience or issuer setting changed. Compare AUTH_TOKEN_AUDIENCE between releases.
