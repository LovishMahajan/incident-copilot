# LOCAL USE ONLY (Artifact 13.5 T2.8): the fake review system needs `git` to build and read the
# reference repository (ops/fakes/review.py). The same Python as the other fakes, plus git.
FROM python:3.12.14-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
USER 65534:65534
