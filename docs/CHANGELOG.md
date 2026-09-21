# Changelog

All notable changes to the Clinic Platform API are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/) and the API uses [semantic versioning](https://semver.org/) for breaking-change majors.

## [Unreleased]

No request or response shape changed. Every row the previous seed produced is still produced, with the same id and the same content.

### Added

- **Seed dataset**: the seed now lives in JSON under `src/clinic_mock/data`, one file per entity (clinics, departments, providers, schedules, patients, slots, appointments), instead of Python lists in `store.py`. It is validated in full when it loads: record formats, references between files, unique phones, and each appointment's slot sitting on its provider's schedule. Every problem is reported at once. `python -m clinic_mock.dataset [DIR]` checks a dataset and prints what it seeds today.
- **Weekly schedules**: open slots are generated from each provider's working hours for `MOCK_SEED_HORIZON_DAYS` (default 21) starting today, so availability never goes stale. The upstream weekday block (2026-09-17 to 2026-10-30, UTC) is kept as a fixed-window schedule with its original `s_<date>_<time>` ids.
- **More data**:
  - 3 clinics (`c_003`, `cl_vinmec_smartcity`, `cl_vinmec_centralpark`).
  - 9 departments (16 in all).
  - 18 providers (25 in all), and every department has at least one.
  - 16 patients per key, including a namesake of `pt_3391` and a child.
  - 14 appointments per key across every clinic except `cl_vinmec`, in the statuses SCHEDULED, CONFIRMED, BOOKED, UNREACHABLE and CANCELLED.
- **Profiles**: rows can belong to a profile other than `base`, seeded only when `MOCK_SEED_PROFILES` names it. `demo` adds a shared call list of 10 appointments and near-term `cl_vinmec` availability across six departments. The stock seed is unchanged for `cl_vinmec`: `slot_91d2` stays its only slot.
- **Settings**: `MOCK_SEED_DATA_DIR` serves another dataset without a code change, and `MOCK_SEED_PROFILES` and `MOCK_SEED_HORIZON_DAYS` control the rest.

### Changed

- `GET /v1/slots` returns slots earliest first (ties by `slot_id`). It used to return them in insertion order.
- A reset reuses the slot rows it has already built for that day, so reseeding thousands of generated slots takes under a millisecond.

## [2.0.0] - 2026-09-15

### Breaking — aligned to AI Health Residency product contract (Rev 1.0)

The mock now matches the contract at [callbot-contract-site.vercel.app](https://callbot-contract-site.vercel.app/) (`AIHR-PC-001`, Rev 1.0). Teams running bots against this mock can be scored directly against the contract.

### Changed

- **Appointment shape** — Listing 3 canonical. `id` → `appointment_id`, `slot: SlotRef` flattened to top-level `clinic_id` / `starts_at` / `ends_at` / `department`. New server-side fields `attempt_count`, `version`. `patient` block now `{patient_id, display_name, verify: {full_name, dob}}` (was `{id, name, phone}`).
- **Patient shape** — `first_name` / `last_name` replaced by `display_name` + `verify` block.
- **AppointmentStatus enum** — dropped `PENDING` / `COMPLETED` / `NO_SHOW`; added `SCHEDULED` (default) and `UNREACHABLE` (terminal). The seven contract end states are the only valid statuses.
- **Cancel reason** — `reason_code` field renamed to `cancel_reason`. Values now `PATIENT_UNAVAILABLE`, `NO_LONGER_NEEDED`, `WENT_ELSEWHERE`, `COST`, `UNSPECIFIED` (Appendix A).
- **Transfer reason** — `reason_code` field renamed to `transfer_reason`. Values now `IDENTITY_FAILED`, `PATIENT_NOT_FOUND`, `OUT_OF_SCOPE`, `CLINICAL_QUESTION`, `NOT_UNDERSTOOD`, `PATIENT_REQUEST`, `SYSTEM_ERROR` (Appendix A). `target_clinic_id` removed (transfer is a status flip, not a sibling-create).
- **Reschedule** — Listing 4 minimal response `{status, new_slot_id, released_slot_id, version}`. Request gains `requested_by: PATIENT|STAFF`. Old slot is re-inserted into `GET /v1/slots` under its original `slot_id`. Error code `RESCHEDULE_SLOT_TAKEN` → `SLOT_TAKEN`.
- **401 BAD_KEY** — was `UNAUTHORIZED`; now matches Appendix A.
- **Time format** — `IsoDateTime` widened to accept `±HH:MM` offsets in addition to `Z`. The `+07:00` fixture times in Listing 3 now validate.
- **`POST /v1/appointments`** — creates at `status = BOOKED`, `version = 1`, `attempt_count = 0` (was `PENDING`).
- **Confirm** — sets `confirmed_at` (timestamp) and `confirmed_via = "callbot"`.

### Added

- **`If-Match` header** — optimistic concurrency on every appointment write (§4.2.3 SHOULD). Mismatch returns `409 VERSION_CONFLICT` with the current `version` in `details`.
- **`Idempotency-Key` on every write** — was only honored on `POST /v1/appointments`. Now honored on confirm / cancel / transfer / reschedule / unreachable.
- **`POST /v1/appointments/{id}/unreachable`** — new endpoint. §1.1.7 / §2.2 UNREACHABLE end state. Idempotent: each call increments `attempt_count` and re-confirms `UNREACHABLE`.
- **Cancel two-step** — `CancelRequest.confirmed: bool` is required (§3.5 SF-05). Without `confirmed: true` the mock returns `409 CONFIRMATION_REQUIRED`.
- **Canonical contract fixtures** — `apt_00417`, `pt_3391`, `slot_91d2`, `slot_77aa`, `cl_vinmec` seeded once under a shared tenant and visible to every caller. Per-team fixtures (`p_12345`, `s_987`, …) keep the per-tenant suffix for isolation testing.

### Removed

- **`/v1/calls/*` surface** — these endpoints are the bot's contract (§4.1), not the mock's (§4.2). The bot owns its own call state.
- **`/_harness/calls/*`, `/_harness/escalations`** — coupled to the removed Calls API.
- **`Call`, `CallAttempt`, `Escalation`, `CallStatus`, `AttemptKind`, `EscalationReason`, `EndOutcome`** — schemas deleted.
- **Webhooks emitter** — already documented as not implemented; removed from the spec.

### Notes

- This is a breaking release from 1.1.0. Any client relying on the legacy `id`, nested `slot`, `first_name` / `last_name`, `PENDING` status, `reason_code` field names, or `RESCHEDULE_SLOT_TAKEN` code must migrate.
- The `UNAUTHORIZED` error code is replaced by `BAD_KEY`. Internal 422 `IDEMPOTENCY_CONFLICT` and 409 `INVALID_STATE_TRANSITION` codes are kept (not in Appendix A but useful).

## [1.1.0] - 2026-09-14

### Added
- **Calls API** — separate lifecycle surface for voice-agent call mechanics (scenarios: identity verification mid-call, transfer-to-staff, no-answer logging, end-of-call).
  - `POST /calls`, `GET /calls/{id}`, `PATCH /calls/{id}`, `POST /calls/{id}/escalate`, `POST /calls/{id}/attempts`, `POST /calls/{id}/end`.
  - New scopes `calls:read`, `calls:write`.
  - New data models: `Call`, `CallAttempt`, `Escalation` and supporting enums (`CallStatus`, `AttemptKind`, `EscalationReason`, `EndOutcome`).
  - New webhook events: `call.started`, `call.escalated`, `call.no_answer`, `call.ended`.
- **Harness expansion** — state-check and snapshot/restore endpoints to support testing workflows; the platform is a mock for testing another service:
  - Read state: `GET /_harness/state`, `GET /_harness/patients`, `GET /_harness/slots`, `GET /_harness/appointments`, `GET /_harness/calls`, `GET /_harness/calls/{id}`, `GET /_harness/escalations`.
  - Snapshots: `GET /_harness/snapshot`, `POST /_harness/snapshot/{id}/restore`.
  - Mutation: `POST /_harness/seed`, `POST /_harness/reset`, `POST /_harness/time-travel`.
- **Langfuse observability contract** — the mock emits OpenTelemetry-compatible traces to Langfuse (`LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_ENVIRONMENT`, `LANGFUSE_TRACES_ENABLED`). Tests read traces from Langfuse directly — **no trace-polling API endpoint**.
  - Per-request span `http.server.request` carrying `langfuse.request.id` (matches `X-Request-Id`).
  - Semantic span events on lifecycle mutations.
  - Always-on redaction of `phone`, `dob`, `notes`, `Authorization`.
- New error codes: `INVALID_ESCALATION_TARGET` (400), `CALL_ALREADY_ENDED` (409).

### Notes
- No backward-incompatible changes from 1.0.0; all additions are new routes/scopes.

## [1.0.0] - 2026-09-14

### Added
- Initial public release of the Clinic Platform API (`v1`).
- Discovery endpoints: `GET /patients`, `GET /slots`.
- Booking & reading: `POST /appointments`, `GET /appointments/{id}`, `GET /appointments`.
- Lifecycle endpoints: `confirm`, `cancel`, `transfer`, `reschedule`.
- Admin endpoints: `POST /_harness/{seed,reset,time-travel}`.
- Standard error envelope (`{error: {code, message, request_id, details?}}`) and machine-readable error code catalog.
- Bearer-token authentication with scope-based authorization (`patients:read`, `slots:read`, `appointments:read`, `appointments:write`, `harness:admin`).
- Cursor-based pagination on list endpoints (`cursor`, `limit` 1..100, default 25).
- `Idempotency-Key` header with 24h replay window and `Idempotent-Replayed: true` response on replay.
- Per-tenant rate limiting with `X-RateLimit-*` headers and `429 RATE_LIMITED`.
- Webhooks: `appointment.{created,confirmed,cancelled,transferred,rescheduled}` with HMAC-SHA256 signing header `X-Clinic-Signature: v1=<hex>` and at-least-once delivery with exponential backoff.
- OpenAPI 3.1 companion document (`openapi.yaml`).
