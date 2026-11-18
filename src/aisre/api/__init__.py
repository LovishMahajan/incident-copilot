"""Artifact 7.4, 13.1: the HTTP API (FastAPI). M0's thin intake, read and timeline routes.

Run with `uvicorn --factory aisre.api.app:create_app`. The API reaches Temporal only through
the outbox (ADR-19, ADR-51): nothing under `aisre.api` imports `temporalio`, and a test keeps
it that way. Full intake, the other routes and real sign-in arrive at M1.
"""
