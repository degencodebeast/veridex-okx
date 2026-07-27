# Veridex Arena — Coolify Deployment Runbook (D-0 static scaffold)

Authoritative operator guide for packaging veridex-arena on **VPS +
Coolify + Postgres**. Authored at Wave 0 (D-0), BEFORE any app
dependency, so deployment is a contract the implementation must meet —
never an afterthought. Discipline modeled on
`agent-rank/scripts/v2_infra.md` (structure only; every hostname,
domain, and credential below is a **placeholder** — D-0 performs NO
real DNS mutation, NO live Coolify changes, NO deploys).

> **Status note.** Repo-owned assets in this pass: `compose.coolify.yml`
> (static skeleton), `apps/web/Dockerfile`, secret-exclusion
> `.dockerignore` files, this runbook, and
> `deploy/coolify/provisioning-inventory.md`. `Dockerfile.api` is
> **I-5-owned** and referenced only as a placeholder; D-1 wires it in.
> Everything under "operator step" requires credentials/network access
> this repo does not have.

---

## 1. Topology

One Coolify project on a single VPS:

```
+---------------------------- VPS (Ubuntu 22.04) -------------------------------+
|                                                                               |
|  +------- Coolify control plane ------------------------------------------+  |
|  |                                                                         |  |
|  |  Project: veridex-arena                                                 |  |
|  |                                                                         |  |
|  |  ┌──────────────┐   ┌────────────────────────┐   ┌──────────────┐       |  |
|  |  │  web         │   │  api-runtime           │   │  postgres    │       |  |
|  |  │  (Next.js)   │──▶│  (API + AgentOS,       │──▶│  (pinned     │       |  |
|  |  │  port 3000   │   │   ONE container, II-4) │   │   16.6)      │       |  |
|  |  └──────────────┘   │  port 8000             │   │  port 5432   │       |  |
|  |                     └────────────────────────┘   └──────────────┘       |  |
|  +-------------------------------------------------------------------------+  |
|                                                                               |
|  Named volumes: postgres-data, wal-spool (WAL_DIR), replay-capture,           |
|                 signal-trials-data (SIGNAL_TRIALS_DATA_DIR)                   |
|  Host path (ro): /srv/veridex/replay-packs/curated (REPLAY_PACK_ROOT)         |
+-------------------------------------------------------------------------------+
```

Boundaries:
- **AgentOS is mounted IN api-runtime per II-4** — it is NOT a separate
  container or service. One process tree serves API + agent runtime.
- **web → api-runtime** over the public API domain (browser calls) —
  client wiring is I-5/Frontend scope, not D-0.
- **api-runtime → postgres** over the Coolify private network only.
- **postgres** is never publicly exposed and never gets a domain.

## 2. Service inventory (single source of truth)

| Service | Image source | Container port | Domain | Persistent storage |
|---|---|---|---|---|
| `web` | `apps/web/Dockerfile` (this repo) | `3000` | `arena.veridex.example` (placeholder) | none |
| `api-runtime` | `Dockerfile.api` — **I-5-owned placeholder**, wired at D-1 | `8000` | `api.veridex.example` (placeholder) | `wal-spool`, `replay-capture`, curated packs (ro) |
| `postgres` | `postgres:16.6-alpine` (pinned official) | `5432` (private only) | none — internal | `postgres-data` |

Named volumes (declared in `compose.coolify.yml`):

| Volume | Mounted in | Purpose |
|---|---|---|
| `postgres-data` | postgres `/var/lib/postgresql/data` | database files |
| `wal-spool` | api-runtime `/var/lib/veridex/wal` | `WAL_DIR` durable event spool (I-4/AC-13) |
| `replay-capture` | api-runtime `/var/lib/veridex/replay-packs/capture` | writable ReplayPack capture root (R-0b/R-2) |
| `signal-trials-data` | api-runtime `/var/lib/veridex/signal-trials` | published season + receipt store (`SIGNAL_TRIALS_DATA_DIR`). **The only durable record that a trial was opened, paid for and settled** — a receipt whose store is gone cannot be verified by anyone, so losing it on redeploy silently voids the Fair-Play claim rather than breaking anything visibly. |

Read-only mount: curated seed packs at
`/srv/veridex/replay-packs/curated` → api-runtime
`/var/lib/veridex/replay-packs/curated:ro`; the runtime resolves the
curated root via `REPLAY_PACK_ROOT` (value injected at D-1).

## 3. VPS provisioning (operator step)

1. Order an Ubuntu 22.04 VPS — minimum 4 GB RAM, 2 vCPU, 50 GB SSD.
2. SSH key-only login; disable password login in `/etc/ssh/sshd_config`.
3. `apt update && apt upgrade -y`.
4. Install Docker: `curl -fsSL https://get.docker.com | bash`.
5. Install Coolify: `curl -fsSL https://get.coolify.io | bash`.
6. Open firewall ports: 22 (SSH), 80 (HTTP→HTTPS redirect), 443
   (HTTPS), Coolify dashboard port (restrict by IP or close after
   setup).
7. Create the curated-pack root on the host:
   ```bash
   mkdir -p /srv/veridex/replay-packs/curated
   chmod 755 /srv/veridex/replay-packs/curated
   ```
8. Verify: `docker ps`, `docker compose version`, Coolify dashboard
   responds.

> Repo cannot do this step — VPS account + SSH access required.

## 4. Coolify project + services (operator step)

In the Coolify dashboard:

1. **Create project** `veridex-arena`.
2. **Add postgres** — managed PostgreSQL (pin image `postgres:16.6-alpine`
   or Coolify's matching managed version) with persistent volume
   `postgres-data`. No domain. Copy the connection string into the
   api-runtime service's `DATABASE_URL` secret (D-1).
3. **Add api-runtime** (Application → Dockerfile build pack):
   - Git repository + branch: this repo.
   - **Base Directory**: `/` (repo root) — `Dockerfile.api` copies the
     `veridex` package, so the build root must be the repo root.
   - **Dockerfile Location**: `Dockerfile.api` (**I-5-owned; exists
     only after D-1 wiring — do not create this service before then**).
   - **Ports Exposes**: `8000`.
   - Attach volumes `wal-spool`, `replay-capture`, `signal-trials-data`, and the read-only
     curated-pack host path per §2.
   - Health check: path + interval wired at D-1 (structural note only).
4. **Add web** (Application → Dockerfile build pack):
   - **Base Directory**: `/apps/web` — the web build context is the app
     dir itself, so `apps/web/.dockerignore` governs it.
   - **Dockerfile Location**: `Dockerfile`.
   - **Ports Exposes**: `3000`.
5. **Auto-deploy**: on push to the release branch (operator choice).

> Repo cannot do this step — Coolify dashboard access required.

## 5. Domains, DNS, HTTPS (operator step — placeholders only)

Coolify ships Traefik; certificates are Let's Encrypt, automatic.

1. A record `arena.veridex.example` → VPS IP; attach to `web`.
2. A record `api.veridex.example` → VPS IP; attach to `api-runtime`.
3. `postgres` gets **no domain** — private network only.
4. Verify after D-1 wiring:
   ```bash
   curl -v https://arena.veridex.example
   curl -v https://api.veridex.example/  # health path wired at D-1
   ```

> `*.veridex.example` values are placeholders. Real domains are chosen
> and mutated by the operator at deploy time, never committed here.

## 6. Private networking

- `api-runtime → postgres` uses Coolify's private Docker network via
  service-name DNS (e.g. `postgres:5432` inside `DATABASE_URL`).
- Port `5432` is never published to the host or a domain.
- Per Coolify's private/internal-services model, services without
  mapped ports/domains stay private and are reachable from sibling
  services by service name.

## 7. Env ownership (names only — NO values in git, ever)

Each service receives ONLY the env vars it owns. Values are injected
via Coolify secrets at **D-1** (with `:?` required-var guards in
compose); this section records ownership so injection is mechanical.
Full secret/config inventory: `deploy/coolify/provisioning-inventory.md`.

| Service | Owns (names only) |
|---|---|
| `api-runtime` | `DATABASE_URL` (secret), `WAL_DIR`, `REPLAY_PACK_ROOT`, venue/provider API keys (secrets; enumerated in the inventory doc), runtime config |
| `web` | `NEXT_PUBLIC_*` build-time args (non-secret by definition — anything `NEXT_PUBLIC_` is shipped to browsers) |
| `postgres` | `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` (secrets) |

Rules:
- Secrets live ONLY in Coolify's secret store. Never in compose, never
  in Dockerfiles, never in git.
- `.dockerignore` (root and `apps/web/`) excludes `**/*.pem`,
  `**/*.key`, wallet/keypair material, and env files from every build
  context, so a built image contains none even if a secret file strays
  into the tree.

## 8. Migrations / DB init

- Postgres starts empty; schema initialization/migrations run from
  api-runtime at startup or via an operator-run one-shot command —
  the mechanism is wired at **D-1** (structural placeholder here).
- Never bake schema or seed data into the postgres image.

## 9. Health checks

- Wiring (paths, intervals, compose `healthcheck:` blocks and Coolify
  health-check config) is **D-1** scope.
- Structural intent: `api-runtime` exposes an HTTP health path checked
  by Coolify; `web` uses its root path; `postgres` uses Coolify's
  managed-DB checks.

## 10. Volumes, backups, restore drill (operator step)

1. Schedule daily `pg_dump` backups of `postgres-data` in Coolify with
   7-day retention.
2. Drill once before demo: restore the latest snapshot into a fresh
   Postgres container, read a sample row back.
3. `wal-spool`, `replay-capture` and `signal-trials-data` are durability surfaces — never
   mount them `tmpfs`, never bind them to ephemeral paths.

## 11. Post-deploy smoke verification (III-1)

`scripts/smoke_public.sh` is the operator-run acceptance instrument for a
LIVE deploy (or a local boot). It is read-only except for one idempotent,
unauthenticated write (`POST /demo/run`, an offline deterministic demo
competition) and never reads or prints a bearer token.

```bash
BASE_URL=https://api.veridex.example ./scripts/smoke_public.sh
```

Checks liveness (`/healthz`), readiness (`/readyz`, durable Postgres + OPS
spool + ReplayPack catalog), the deploy-auth fail-closed boundary (an
unauthenticated control-plane write must be 401), and that a replay-mode
run is reachable with no operator help (the judge cold-hit path).

**AC-13 restart-durability** (I-4/AC-13 — the WAL-spool → Postgres path
keeping the durable run store intact across a restart) is a separate
BINARY GATE, run explicitly around a restart:

```bash
./scripts/smoke_public.sh --ac13-pre     # records a run_id
# now restart the api-runtime service in Coolify (or `docker compose restart api-runtime`)
./scripts/smoke_public.sh --ac13-post    # re-queries the SAME run_id; MET or UNMET
```

`AC13 UNMET` means the durable run was lost across the restart — check
that `DATABASE_URL` (not an in-memory fallback) and the `wal-spool`
volume (§2) are actually wired on the deployed service, not just present
in this runbook. See the script header for the exact routes and rationale.

Trust boundary: `AC13_MET` is only as strong as the restart it was run
around — a no-op `RESTART_CMD` (or an operator restart that didn't
actually happen) reports MET without proving anything, since the script
has no process-uptime endpoint to verify the restart itself occurred.
Prefer `--ac13-pre` / `--ac13-post` around a restart you have verified.

## 12. Negative scope (what D-0 does NOT do)

- NO `Dockerfile.api` — I-5-owned; D-1 wires it (graph: D-0 + I-5 → D-1 → II-11).
- NO env values, no `:?` guards, no healthchecks, no db-init — D-1.
- NO real DNS mutation, no live Coolify changes, no deploys, no
  production credentials.
- NO app wiring (`apps/web` API/WS clients are I-5/Frontend scope).
