# Reference application: orders-api (M2, T2.3)

A runnable copy of the service in the reference incident (`tests/fixtures/reference-incident/`), small enough to test in the sandbox with no network and nothing to install: standard library only, tests on `unittest`.

- `orders/pricing.py`: `compute_totals` parses `ORDER_TAX_RATE_BPS` with `int()` on every order.
- `orders/app.py`: `POST /v1/orders` and `GET /healthz`, which reads no setting. An unhandled error is logged with its traceback and answered with 500.
- `deploy/production.env`: the configuration. Line 14 is `ORDER_TAX_RATE_BPS`.
- `tests/unit`, `tests/integration` (the service over HTTP on loopback), `tests/smoke`: the suites the harness runs.

The sandbox checks out a revision with `git archive`, so the application is a git repository with the incident's history. `build_repo.py` makes it, with fixed authors and dates, so the commit IDs are the same everywhere:

```bash
python ops/reference-app/build_repo.py /path/to/orders-api   # prints each tag and its commit
```

| Tag | Commit message | `ORDER_TAX_RATE_BPS` | Tests |
| --- | --- | --- | --- |
| `cfg-41`, `v2.14.0` | orders-api 2.14.0 | `825` | Pass |
| `cfg-42`, `v2.14.1` (`main`) | Update tax rate config to match finance sheet (8.25%) | `8.25` | Integration and smoke fail with `ValueError: invalid literal for int() with base 10: '8.25'` |

The fixture's commit `7d3e91a` can't be reproduced: a commit ID is a hash of the content. Everything is synthetic.
