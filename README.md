# olah-cache-manager

A small web UI to **list and delete individual cached repositories** from an
[olah](https://github.com/vtuber-plan/olah) HuggingFace mirror cache.

olah is a self-hosted HF mirror that caches repos on demand and prunes them
automatically (`cache-size-limit` + `cache-clean-strategy = LRU|FIFO|LARGE_FIRST`,
applied on an hourly size scan). It ships a read-only inventory page at `/repos`
(repo names only) and **no way to delete a specific repo on demand**. This service
fills that gap: it shows every cached repo with its size and last-access time, and
lets you evict any of them immediately so the space is free for the repos you
actually need.

## What it does

- Lists cached repos across both olah trees — `api/{models,datasets,spaces}/<org>/<repo>`
  (API metadata) and `files/{models,datasets,spaces}/<org>/<repo>` (the file/block
  cache, i.e. the bulk) — with **size**, **entry count**, and **last access** (the
  newest `meta.lock`/file mtime, the same signal olah's LRU eviction sorts on).
- `POST /delete` evicts a repo **using olah's own eviction primitives**, so the
  behaviour is identical to olah's own cleanup:
  - whole cache **entries** (dirs containing `meta.bin`) are removed — never
    individual `blocks/block_*.bin` files, which would corrupt an entry;
  - each entry is removed only after a non-blocking exclusive lock on its
    `meta.lock`; an entry olah is mid-open/create/resize on is **skipped**, not
    corrupted;
  - loose cached API responses (`*.json` + `*.json.body`) are removed too.
- Deletion is **off by default** (`MANAGER_DELETE_ENABLED=false`); the UI still
  lists repos read-only until you enable it.

## Design rationale

### Why a separate image instead of patching olah

olah already has a web server (FastAPI) and a `/repos` page, so the obvious
question is "why not just add the delete button to olah itself?". There are two
ways to do that, and this repo deliberately picks a *separate image* so the
deployment can choose either without forking olah:

- **Integrated launcher (same container, same port).** Import olah's FastAPI app
  and append our router:

  ```python
  from olah.server import app
  from manager import router
  app.include_router(router)
  ```

  One container, one service, one ingress, no second port. But the delete UI now
  shares olah's process: a bug or a crash in the admin UI can take the mirror
  down, and the routes are reachable on olah's ingress, which **has no auth**.

- **Sidecar (separate container, same pod).** This is the recommended deployment:
  - **Blast-radius isolation** — an admin-UI bug cannot take down the mirror.
  - **Clean auth boundary** — olah's ingress is unauthenticated, so exposing a
    *delete* endpoint there would let anyone wipe the cache. The sidecar gets its
    own ingress behind authentication.
  - **No overlaying olah internals** — Renovate bumping the olah base can't
    collide with our code.
  - Independent resource limits and hardening.

  Cost: one extra container + one service/ingress.

Because both options consume the **same image**, this repo does not force the
choice — build the image here, then deploy it as a sidecar, or (if you prefer
fewer moving parts and add path-scoped auth to olah's ingress) wire it in as an
integrated launcher.

### Why the image is built `FROM xiahan2019/olah`

The delete logic must match olah's cache format (v10 entries: `meta.bin`,
`chunks.crc`, `blocks/`, plus the `meta.lock` locking convention) **exactly**.
That logic already ships inside olah as `olah.utils.disk_utils`, so building on the
olah image lets this service call the real thing instead of re-implementing it:

```python
from olah.utils.disk_utils import (
    _try_evict_entry,            # lock meta.lock LOCK_EX|LOCK_NB, then rmtree
    collect_cache_units,         # one tree walk -> sizes + entries + loose JSON
    convert_bytes_to_human_readable,
)
```

Trade-off: the image is larger than a `python-slim` build and depends on olah
internals (including the private `_try_evict_entry`). That coupling is accepted
because correctness beats image size here, and Renovate keeps the base pinned and
current. If the coupling ever bites, the alternative is a slim base with the
~130 lines of `collect_cache_units` / `_try_evict_entry` vendored in.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `MANAGER_REPOS_PATH` | `/data/repos` | olah cache root (the mirror's `repos-path`) |
| `MANAGER_DELETE_ENABLED` | `false` | set `true` to enable `POST /delete` |

## Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | HTML list of cached repos |
| `GET` | `/healthz` | liveness/readiness JSON |
| `POST` | `/delete` | form body `rtype`, `org`, `repo` → evict repo (403 if disabled) |

## Run locally

The image expects an olah cache tree. To try it against a throwaway tree:

```sh
docker build -t olah-cache-manager .
mkdir -p /tmp/repos/api/models/acme/demo/cache-entry
printf 'OLAH' > /tmp/repos/api/models/acme/demo/cache-entry/meta.bin
touch /tmp/repos/api/models/acme/demo/cache-entry/meta.lock
docker run --rm -p 8091:8091 -e MANAGER_DELETE_ENABLED=true \
  -v /tmp/repos:/data/repos:rw olah-cache-manager
# open http://localhost:8091
```

## Deploy as a sidecar (Flux / bjw-s app-template)

Add to the olah HelmRelease. The pod already mounts the cache at `/data/repos`
via `persistence.data.globalMounts`, so the sidecar inherits it:

```yaml
    controllers:
      olah:
        containers:
          manager:
            image:
              repository: ghcr.io/niki-on-github/olah-cache-manager
              tag: "latest"   # pin a digest/tag in production
            env:
              - { name: MANAGER_REPOS_PATH,     value: /data/repos }
              - { name: MANAGER_DELETE_ENABLED, value: "true" }
    service:
      manager:
        controller: olah
        ports:
          http: { port: 8091 }
    ingress:
      manager:
        className: traefik
        annotations:
          traefik.ingress.kubernetes.io/router.entrypoints: websecure
          # REQUIRED: olah's own ingress has no auth; never expose delete unauthenticated
          traefik.ingress.kubernetes.io/router.middlewares: "authentication-auth@kubernetescrd"
        hosts:
          - host: olah-manager.${SECRET_DOMAIN}
            paths:
              - { path: /, pathType: Prefix, service: { identifier: manager, port: 8091 } }
        tls:
          - hosts: [olah-manager.${SECRET_DOMAIN}]
```

## Build & release

`.github/workflows/build.yaml` builds multi-arch (`linux/amd64,linux/arm64`) and
pushes `ghcr.io/niki-on-github/olah-cache-manager` on push to `main` and on `v*`
tags (`latest`, semver, and `sha-*` tags). Renovate (`renovate.json`) keeps the
olah base image and the GitHub Action versions pinned and up to date.

## License

MIT — see [LICENSE](LICENSE). Reuses eviction helpers from
[olah](https://github.com/vtuber-plan/olah) (MIT).
