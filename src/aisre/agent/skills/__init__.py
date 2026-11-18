"""Artifact 9.2, 9.3, 13.5 T1.14: the model-backed skills `hypothesise`, `diagnose` and
`review_diagnosis`.

`reasoning` is pure: the context each skill assembles, the request it sends through the Model
Gateway and the checks its answer must pass. `activities` holds the Temporal activities, which
read the record through a `SkillStore` and call the gateway; `store` is that store over
PostgreSQL (T1.15).
"""
