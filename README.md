# clinic-mock

Clinic mock aligned to the **AI Health Residency product contract, Rev 1.0**
([callbot-contract-site.vercel.app](https://callbot-contract-site.vercel.app/)).
FastAPI in-process; bearer-key auth; each API key is fully isolated from every
other; canonical contract fixtures (`apt_00417`, `pt_3391`, `slot_91d2`, `cl_vinmec`)
are visible to every caller; optional Langfuse tracing.

The full contract lives in [`docs/APIs.md`](docs/APIs.md) (authoritative human
spec) and [`docs/openapi.yaml`](docs/openapi.yaml) (machine-readable). This
README is the fastest path from zero to a working request.

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

Keys are registered via `MOCK_API_KEYS` in `.env`:

```env
MOCK_API_KEYS=$KEY,$KEY_ANOTHER
```

Each key resolves to exactly one isolated data scope, auto-derived as
`t_<sha256(key)[:8]>` — stable across restarts, opaque, and unique per
key. You never configure the scope; the platform picks it. Entries
missing the `sk_` prefix are silently dropped. No expiry, no rotation —
keys live as long as they're in the env.

The legacy explicit form (`scope:key`) still works if you need a
human-readable scope id (e.g. for cross-referencing with an external
system):

```env
MOCK_API_KEYS=acme:$KEY_ACME,globex:$KEY_GLOBEX
```

The default `.env` ships five demo keys for testing isolation; replace
them with your own before any shared deployment.

---

## Data Isolation

Every valid key sees **only its own data**. The mock enforces this on every
read and mutation — there is no global view, no admin super-key, no shared
`/state`.

```bash
# key 1 finds its own patient
curl -s "http://localhost:8000/v1/patients?phone=0912345678" \
  -H "Authorization: Bearer $KEY_A" | jq '.data | length'
# 1

# key 1 does NOT find key 2's patient — same phone query, different key
curl -s "http://localhost:8000/v1/patients?phone=0987654321" \
  -H "Authorization: Bearer $KEY_A" | jq '.data | length'
# 0
```

**Cross-key access returns `404 NOT_FOUND`, not `403 FORBIDDEN`** — existence
is hidden, not forbidden. The isolation scope is server-side only and never
appears in response payloads.

The same scoping applies to `/_harness/*`: `GET /_harness/patients` shows only
the caller's data, not all of it.

---

## Common Workflows

Set the key once per shell session:

```bash
export KEY=your-api-key-here
export HOST=http://localhost:8000
```

### Find a patient (inbound call, §1.1.9)

```bash
curl -s "$HOST/v1/patients?phone=0912345600" \
  -H "Authorization: Bearer $KEY" | jq '.data[0].patient_id, .data[0].verify'
```

### Book an appointment (§1.1.10)

```bash
# 1. Find an open slot
curl -s "$HOST/v1/slots?clinic_id=cl_vinmec&from=2026-10-14T00:00:00Z&to=2026-10-15T00:00:00Z" \
  -H "Authorization: Bearer $KEY" | jq '.data[0].slot_id'

# 2. Book it
curl -s -X POST "$HOST/v1/appointments" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{"patient_id": "pt_3391", "slot_id": "slot_91d2"}'
```

### Lifecycle: confirm / cancel / transfer / reschedule / unreachable

```bash
# §1.1.3 — confirm sets CONFIRMED + confirmed_at + confirmed_via=callbot
curl -s -X POST "$HOST/v1/appointments/apt_00417/confirm" \
  -H "Authorization: Bearer $KEY" \
  -H "If-Match: 3" \
  -H "Idempotency-Key: $(uuidgen)"

# §1.1.4 + §3.5 SF-05 — confirmed:true is required (one ambiguous turn is not enough)
curl -s -X POST "$HOST/v1/appointments/apt_00417/cancel" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -H "If-Match: 3" \
  -d '{"cancel_reason": "PATIENT_UNAVAILABLE", "confirmed": true}'

# §1.1.5 — transfer (status flip, no sibling appointment)
curl -s -X POST "$HOST/v1/appointments/apt_00417/transfer" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -H "If-Match: 3" \
  -d '{"transfer_reason": "CLINICAL_QUESTION"}'

# §1.1.6 / Listing 4 — atomic slot swap; minimal response
curl -s -X POST "$HOST/v1/appointments/apt_00417/reschedule" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -H "If-Match: 3" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{"new_slot_id": "slot_91d2", "requested_by": "PATIENT"}'
# {"status":"RESCHEDULED","new_slot_id":"slot_91d2","released_slot_id":"slot_77aa","version":4}

# §1.1.7 / §2.2 — unreachable (idempotent; each call increments attempt_count)
curl -s -X POST "$HOST/v1/appointments/apt_00417/unreachable" \
  -H "Authorization: Bearer $KEY" \
  -H "If-Match: 3"
```

The Calls API (`/v1/calls/*`) is the bot's contract (§4.1), not the mock's. Bots expose those endpoints themselves; the mock only carries appointment state.

---

## Harness (Test Isolation)

`/_harness/*` is scoped to the caller's key — it sees only the caller's data.
Use these for test setup / teardown:

```bash
H="$HOST/_harness"
AUTH="-H Authorization:Bearer\ $KEY"

# Seed canonical fixtures (idempotent; resets first)
curl -s -X POST "$H/seed" $AUTH

# Snapshot current state → restore later without re-seeding
SID=$(curl -s "$H/snapshot" $AUTH | jq -r .snapshot_id)
# ... run your test, mutate freely ...
curl -s -X POST "$H/snapshot/$SID/restore" $AUTH

# Full reset
curl -s -X POST "$H/reset" $AUTH

# Advance the clock for no-show / time-window tests
curl -s -X POST "$H/time-travel" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"seconds": 3600}'
```

`/_harness/state` returns the entire caller's data scope at once — useful
for post-test assertions.

---

## Seed Data

The data the mock starts with lives in JSON under
[`src/clinic_mock/data/`](src/clinic_mock/data/), not in code. The mock loads it
on startup, on `/_harness/seed` and on `/_harness/reset`. Adding a doctor, a
clinic, a patient or an appointment means editing data, and no Python changes.

| File | Holds |
| :--- | :--- |
| `clinics.json` | Clinics: id, name, city, district |
| `departments.json` | Specialties; `name` is what slots and appointments carry |
| `providers.json` | Doctors, each at one clinic in one department |
| `schedules.json` | Weekly working hours; open slots are generated from them |
| `patients.json` | People the phone lookup finds (`verify` is what the bot checks) |
| `slots.json` | One-off open slots at fixed times |
| `appointments.json` | Existing bookings |

The stock seed has:

- **Catalog:** 6 clinics, 16 departments and 25 providers.
- **Per API key:** 35 patients, about 2,700 open slots over the next 21 days,
  and 14 appointments across five clinics in the statuses SCHEDULED, CONFIRMED,
  BOOKED, UNREACHABLE and CANCELLED.
- **Shared by every key:** the contract fixtures `pt_3391`, `apt_00417` and
  `slot_91d2`.

`cl_vinmec` has no other open slot, because the Appendix A reschedule depends
on `slot_91d2` being the only offer. Run `python -m clinic_mock.dataset` to
see today's numbers.

### Scope and profile

Clinics, departments and providers are always loaded. Every other row takes two
optional keys:

- **`scope`**: `tenant` (the default) copies the row once per API key and
  appends the key's 4-character hash to its id (`apt_seed_05_f970`), so keys
  never see each other's writes. `shared` seeds one copy that every key sees.
- **`profile`**: `base` (the default) is the stock seed and is always loaded.
  A row in any other profile is seeded only when `MOCK_SEED_PROFILES` names it.
  `demo` adds the callbot console's call list (`apt_demo_1` … `apt_demo_10`)
  and near-term `cl_vinmec` availability across six departments.

### Dates stay current

A schedule generates slots from today for `MOCK_SEED_HORIZON_DAYS`. An
appointment can name a slot relative to today instead of a date:

```json
{"appointment_id": "apt_seed_05", "patient_id": "p_sample_18", "provider_id": "pr_301",
 "status": "SCHEDULED", "working_day": 1, "time": "10:00"}
```

That is `pr_301`'s 10:00 slot on the first day after today that `pr_301`
works. The booking takes that slot out of the open list, unless it is
`CANCELLED`, and `POST /cancel` puts it back under the same id. The contract
fixtures and the upstream fixed-date rows use fixed times and never move.
Where a slot or appointment with a fixed time and a generated slot overlap for
the same doctor, the fixed row wins.

### Adding a doctor

Add the doctor to `providers.json` and their hours to `schedules.json`:

```json
{"id": "pr_305", "name": "Bác sĩ Trần Minh Khôi", "clinic_id": "c_003", "department": "Mắt"}
```

```json
{"provider_id": "pr_305", "weekdays": [0, 1, 2, 3, 4],
 "sessions": [["08:00", "11:30"], ["13:30", "16:30"]], "slot_minutes": 30}
```

`weekdays` counts from Monday = 0. Times are clinic-local (`+07:00`) unless
`utc_offset` says otherwise. A schedule with both `valid_from` and `valid_to`
is a fixed block; any other schedule rolls with the calendar.

### Validation

A dataset is checked in full when it loads, and the mock refuses to start with
a bad one. The check covers:

- formats per record: phones, dates, clock times, statuses and reason codes;
- misspelt keys;
- references between files;
- duplicate ids or phones;
- a doctor double-booked by two schedules;
- an appointment whose time is not on its doctor's schedule.

Every problem is listed at once. Records may carry a `"note"`, because JSON has
no comments.

```bash
uv run python -m clinic_mock.dataset                      # packaged dataset, today
uv run python -m clinic_mock.dataset path/to/dataset --profiles base,demo
```

### Settings

| Variable | Default | Meaning |
| :--- | :--- | :--- |
| `MOCK_SEED_DATA_DIR` | packaged `data/` | Serve another dataset, such as a larger one for load tests |
| `MOCK_SEED_PROFILES` | `base` | Profiles to seed, comma-separated; `base` is always included |
| `MOCK_SEED_HORIZON_DAYS` | `21` | Days of generated availability, counting today |

Loading is cached per directory. The rows for a given day are built once and
reused on every reset, so a larger dataset makes startup slower but not the
per-test reseed.

---

## Phone Numbers

Phones are VN-local 10-digit, validated as
`^(02|03|05|07|08|09)\d{8}$`:

| Prefix | Carrier |
| :--- | :--- |
| `02` | Landline |
| `03`, `09` | Viettel mobile |
| `05` | Vietnamobile |
| `07` | Mobifone |
| `08` | Vinaphone |

`+84` (E.164) format is rejected — convert before calling.

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

---

## Where Things Live

```
src/clinic_mock/
├── main.py            # entry point: `uv run dev` / `uv run prod`
├── app.py             # FastAPI app, middleware (auth, request-id), custom OpenAPI
├── auth.py            # bearer-key parser; per-key isolation registry
├── config.py          # pydantic-settings (loads .env)
├── errors.py          # ApiError envelope + exception handlers (Appendix A codes)
├── lifecycle.py       # appointment state-transition guards (§2.2)
├── logger.py          # loguru setup
├── routes.py          # all v1 + harness + health routes
├── schemas.py         # Pydantic models matching contract §2.2 + Appendix A
├── dataset.py         # seed dataset: record types, loading, validation, CLI
├── seeding.py         # dataset -> one day's rows (schedules -> open slots)
├── store.py           # in-memory db; seed_default() fills it from the dataset
├── tracing.py         # Langfuse + FastAPI OTel instrumentation
└── data/              # the seed dataset, one JSON file per entity (see "Seed Data")
```

The product contract at
[callbot-contract-site.vercel.app](https://callbot-contract-site.vercel.app/)
is the source of truth for the contract. The code is the source of truth for
behavior. `docs/APIs.md` is the human-readable companion to both.
