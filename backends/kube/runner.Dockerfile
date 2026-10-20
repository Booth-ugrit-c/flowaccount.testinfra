ARG RUNNER_BASE
FROM ${RUNNER_BASE}
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 git \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))'
USER runner
