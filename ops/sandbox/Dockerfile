# Artifact 11.4, 11.5 (T2.4, ADR-71): the sandbox image. The trusted harness on a read-only
# image filesystem, outside the workspace; Python's standard library is the test runner the
# reference application needs, so nothing is installed and nothing needs the network at run time.
# The base is pinned by digest; the image itself is named by its content-addressed ID (locally)
# or its registry digest (test host): AISRE_SANDBOX_IMAGE, never a tag.
FROM python:3.12.14-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

COPY --chmod=0555 harness/ /harness/
LABEL aisre.harness.version="harness@1"
ENV PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp
USER 65534:65534
WORKDIR /workspace
ENTRYPOINT []
CMD ["/harness/run"]
