# clinic-mock — v1.0.0

**v1.0.0** — first stable release aligned to the **AI Health Residency product
contract, Rev 1.0** ([callbot-contract-site.vercel.app](https://callbot-contract-site.vercel.app/)).

FastAPI in-process; bearer-key auth with three recommended keys
(dev / test / prod); per-key tenant isolation; canonical contract fixtures
(`apt_00417`, `pt_3391`, `slot_91d2`, `cl_vinmec`) visible to every caller;
optional Langfuse tracing; `/_harness/*` test-scoping endpoints.

The full contract lives in [`docs/APIs.md`](docs/APIs.md) (authoritative human
spec) and [`docs/openapi.yaml`](docs/openapi.yaml) (machine-readable). The
contract itself is preserved verbatim in [`docs/product-contract.md`](docs/product-contract.md).

---

## Quickstart

```bash
# 1. Install
uv sync

# 2. Configure
cp .env.example .env  # or edit .env directly

# 3. Run (dev — hot reload)
uv run dev
# or production-style single process
uv run prod

# 4. Hit it
curl -s http://localhost:8000/health
# {"status":"ok"}

# 5. Browse the contract
open http://localhost:8000/docs
```

The first `uv run` resolves deps and installs the `dev` / `prod` console
scripts defined in `pyproject.toml`. `--reload` is automatic for `dev`.

---

## Authentication

Every request (except `/health`, `/docs`, `/openapi.json`, `/redoc`) needs a
bearer key:

```bash
curl http://localhost:8000/v1/patients?phone=0912345678 \
  -H "Authorization: Bearer $KEY"
```

Keys are registered in `.env`. The recommended layout is three named keys
covering the typical dev/test/prod split — each one becomes its own tenant
scope, fully isolated from the others:

```env
MOCK_API_KEYS=dev:sk_dev_replace_me,test:sk_test_replace_me,prod:sk_prod_replace_me
```

| Key | Use |
|---|---|
| `dev` | Local development, fixtures you don't mind losing. |
| `test` | CI / shared test environment. |
| `prod` | Any environment where data must survive a restart. |

Each key resolves to one isolated data scope, auto-derived as
`t_<sha256(key)[:8]>` — stable across restarts, opaque, unique per key.
The platform picks the scope; you don't configure it. Cross-tenant access
returns `404 NOT_FOUND` (existence hidden). Entries missing the `sk_` prefix
are silently dropped. No expiry, no rotation — keys live as long as they're
in the env.

The bare-key form (`MOCK_API_KEYS=sk_dev_xxx,sk_test_xxx`) still works if
you don't want the named scope; the auto-derived scope id just becomes
opaque.

---

## Seed data

Seed data is loaded from `src/clinic_mock/data`. The `base` profile creates
patients and appointments for every configured API key and generates open
slots from provider schedules for a rolling period starting on the current
date.

```env
# Leave empty to use src/clinic_mock/data
MOCK_SEED_DATA_DIR=
MOCK_SEED_PROFILES=base
MOCK_SEED_HORIZON_DAYS=21
```

Use `base,demo` when you also want the shared call-list fixtures and more
near-term availability at Vinmec Times City:

```env
MOCK_SEED_PROFILES=base,demo
```

Validate the configured JSON dataset and preview today's generated counts:

```bash
python -m clinic_mock.dataset
```

The validator checks duplicate IDs and phone numbers, references between
clinics, departments, providers, schedules, slots and appointments, provider
working hours, and overlapping provider appointments.

---

## Observability

When `LANGFUSE_TRACES_ENABLED=true` (default) and
`LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST` are set, every
incoming request emits an OTel span to Langfuse with attributes documented in
[`docs/APIs.md` §14](docs/APIs.md#14-observability--langfuse-traces).

Disable for offline tests:

```env
LANGFUSE_TRACES_ENABLED=false
```

Spans are dropped, not buffered.

---

## Deployment

A `Dockerfile` ships in the repo (builder + runtime stages, non-root user,
uv-managed venv). The runtime image respects `PORT` (Vercel convention) over
`APP_PORT`:

```bash
docker build -t clinic-mock .
docker run -p 8000:8000 --env-file .env clinic-mock
```

On Vercel the build reads `PORT=80` from the platform; locally it falls back
to `APP_PORT=8000` (or the default `8000`).
