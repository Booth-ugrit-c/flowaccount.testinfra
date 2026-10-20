# workload

Synthetic CI job: burns a requested amount of CPU time on N worker processes, holds resident memory, and starts generic service containers. Same binary on every runner design; wall time and queue wait come from the platform under test.

```
python -m workload run --job-json '{"id":"unit-017","lane":"unit","cpu_seconds":120,"threads":4,"memory_mib":1024,"services":["sql","cache"],"label":"x"}'
python -m workload run --job-file job.json
python -m workload check specs.json        # JSON array; exit 2 if any spec is invalid
python -m workload leftovers               # containers named wl-trial-*; exit 1 if any
```

`run` prints one `workload-result {json}` line on stdout (progress goes to stderr) and exits 0 only when `ok` is true. Exit 2 means an invalid spec, 1 a runtime failure, 143 a SIGTERM.

Log prefixes for grep: result line `workload-result `, progress lines `workload: `; both start with `workload`.

Tests: by default only the `cache` service is started for real; set `WORKLOAD_DOCKER_FULL=1` to also pull and start the other four types (about 7 GB of images).

Services (images pinned by digest, readiness probed with the image's own client):

| type | image |
|---|---|
| sql | `postgres:17-alpine@sha256:b0f9560a2de083e2cc7382e75f808c7381a32852a7ec49117deedb300e552b24` |
| document | `mongo:8.0@sha256:4a4ab5d4834207389e0d7270fac988e21ffedccecba77991a552f755df229907` |
| cache | `redis:8-alpine@sha256:3811787313eba226a2ef38658c6ccb91cd5e110edc89c37767de373120a0e5a0` |
| search | `docker.elastic.co/elasticsearch/elasticsearch:8.19.5@sha256:86d0a30f420470d9cd4102cdf380c5a458bd6ccbbd3f54f7332d3b0b562431b8` |
| queue | `rabbitmq:4.2-alpine@sha256:2ec385e7399a5e0d6fc267304fb711ac78ec0b05dc0a64c4d7501f70b15d66a2` |
