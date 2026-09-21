# Clinic Platform API Specification

Version: **v2.0.0** · Last revised: 2026-09-15 · Companion: [`openapi.yaml`](openapi.yaml) · [`CHANGELOG.md`](CHANGELOG.md)

Source of truth: the AI Health Residency product contract, Rev 1.0 ([callbot-contract-site.vercel.app](https://callbot-contract-site.vercel.app/)).

## 0. Overview

The Clinic Platform API is the surface that the team's bot calls into. It is a public, versioned HTTP API in a single scope on the same base URL — appointment and discovery endpoints. The bot's own `/v1/calls/*` endpoints (contract §4.1) live on the bot service and are out of scope here.

| Property | Value |
| :--- | :--- |
| Base URL (production) | `https://api.clinic.example/v1` |
| Base URL (sandbox) | `https://sandbox.api.clinic.example/v1` |
| Transport | HTTPS only; HTTP requests are rejected at the edge. |
| Encoding | `Content-Type: application/json; charset=utf-8` |
| Stability | `v2` is stable. Breaking changes ship as `v3`; non-breaking additions ship within `v2`. |
| Time format | RFC 3339 / ISO-8601 — UTC (`Z`) **or** offset (`+07:00`). Date-only fields are `YYYY-MM-DD`. |

## 1. Conventions

### 1.1 Request Headers

| Header | Required | Description |
| :--- | :--- | :--- |
| `Authorization` | yes (except `/health`, `/docs`, `/openapi.json`, `/redoc`) | `Bearer <api_key>` — see [Authentication](#2-authentication). |
| `Content-Type` | on requests with a body | `application/json; charset=utf-8`. |
| `Idempotency-Key` | **MUST on every write** (§4.2.3) | Stable per-key token, ≤ 255 chars. Replays with the same payload return the cached response; same key + different payload returns `422 IDEMPOTENCY_CONFLICT`. |
| `If-Match` | SHOULD on every write (§4.2.3) | Last-read `version` of the appointment (integer). Mismatch returns `409 VERSION_CONFLICT`. |
| `X-Request-Id` | recommended | Client-supplied correlation id. Echoed in the response and the error envelope. |

### 1.2 Response Headers (every response)

| Header | Description |
| :--- | :--- |
| `Content-Type` | `application/json; charset=utf-8` |
| `X-Request-Id` | Server-issued if the client omitted one. |
| `Idempotent-Replayed` | `true` on `/v1/appointments` when the response was served from the idempotency cache. |

## 2. Authentication & Data Isolation

The mock uses **mock-grade bearer-token auth**: a single API key per caller, no JWT, no signature, no per-scope grants. Every valid key has full access to its own data and no other caller's data. A real implementation would validate HS256 signatures against an IDP — out of scope for this mock.

### 2.1 Request

**All endpoints require a bearer token**, including `/_harness/*`. Only the docs endpoints (`/docs`, `/openapi.json`, `/redoc`) and `/health` skip auth.

```
Authorization: Bearer <api_key>
```

* `<api_key>` must start with the prefix `sk_`.
* Keys are registered via the `MOCK_API_KEYS` env var as a comma-separated list (`sk_xxx,sk_yyy,…`). Each key is automatically scoped to its own isolated data.
* The legacy explicit form (`scope:sk_xxx`) is still accepted if a human-readable scope label is needed.
* No expiration, no rotation, no revocation.

### 2.2 Isolation Guarantees

* A caller **cannot list, read, or mutate another caller's data**. Cross-key access is hidden, not forbidden — see §2.3.
* The internal isolation scope is **never returned in response payloads**. It exists server-side only and is marked `exclude=True` on every response model.
* `/_harness/*` is **per-key**, not global.
* **Canonical contract fixtures** (e.g. `apt_00417`, `pt_3391`, `slot_91d2`, `cl_vinmec`) are seeded once under a shared sentinel tenant and visible to every caller. Per-team fixtures remain isolated.

### 2.3 Failure Modes

| HTTP | Code | When | Response Header |
| :---: | :--- | :--- | :--- |
| `400` | `INVALID_REQUEST` | Request body or query failed validation. `details[]` names offending fields. | — |
| `401` | `BAD_KEY` | Token missing, malformed, or unknown to `MOCK_API_KEYS`. | `WWW-Authenticate: Bearer realm="clinic-mock"` |
| `404` | `NOT_FOUND` | Resource does not exist or is not visible to the caller. | — |
| `409` | `SLOT_TAKEN` | Referenced `slot_id` is already reserved. | — |
| `409` | `VERSION_CONFLICT` | `If-Match` does not match the current `version`. | — |
| `409` | `CONFIRMATION_REQUIRED` | Cancel was issued without the explicit second confirmation (§3.5 SF-05). | — |
| `409` | `INVALID_STATE_TRANSITION` | Action not allowed from the current appointment status. | — |
| `422` | `IDEMPOTENCY_CONFLICT` | Same `Idempotency-Key` reused with a different payload. | — |
| `429` | `RATE_LIMITED` | Reserved for future per-key rate limiting; not currently returned. | `Retry-After` (future) |
| `503` | `UPSTREAM` | Reserved for upstream failures; not currently returned. | `Retry-After` (future) |

### 2.4 Operator Configuration

| Env Var | Required | Description |
| :--- | :--- | :--- |
| `MOCK_API_KEYS` | yes for multi-caller testing | Comma-separated `sk_xxx` keys. Each key is auto-isolated. |

## 3. Endpoints

### 3.1 Discovery & Lookup

#### `GET /v1/patients`
Finds patient records by phone. Used during inbound calls (§1.1.9) to identify the caller before booking.
* **Query:** `phone` (string, required, `^(02|03|05|07|08|09)\d{8}$`), optional `cursor`, `limit`.
* **Response `200 OK`:** Paginated `Patient` envelope. Each patient carries `display_name` and `verify: {full_name, dob}`.

#### `GET /v1/slots`
Retrieves genuinely open, bookable time slots. The **only** legal source for presenting availability to a caller (§1.1.6).
* **Query:** `clinic_id` (required), `from` (RFC 3339, required), `to` (RFC 3339, required, `to > from`, `to - from ≤ 14d`), optional `cursor`, `limit`.
* **Response `200 OK`:** Paginated `Slot` envelope, earliest `start_time` first (ties by `slot_id`), so cursors page stably. Times may carry a `±HH:MM` offset (`+07:00` for Vietnam).

### 3.2 Booking & Reading

#### `POST /v1/appointments`
Creates one appointment, consuming an open slot (§1.1.10 inbound booking).
* **Headers:** `Idempotency-Key` (MUST), `If-Match` (SHOULD, but version starts at 1 so typically absent on create).
* **Request body:** `{ "patient_id": "pt_3391", "slot_id": "slot_91d2" }`
* **Response `201 Created`:** `Appointment` with `status = BOOKED`, `version = 1`, `attempt_count = 0`.
* **Errors:** `400 INVALID_REQUEST`, `401 BAD_KEY`, `404 NOT_FOUND` (patient or slot), `422 IDEMPOTENCY_CONFLICT`.

#### `GET /v1/appointments/{id}`
Reads a single appointment — Listing 3 canonical shape.
* **Response `200 OK`:** `Appointment`.
* **Errors:** `401 BAD_KEY`, `404 NOT_FOUND`.

#### `GET /v1/appointments?date=&clinic_id=`
Generates the call list for a clinic on a given date, ordered by `starts_at`.
* **Query:** `date` (`YYYY-MM-DD`, required), `clinic_id` (required), optional `cursor`, `limit`.
* **Response `200 OK`:** Paginated `Appointment` envelope.

### 3.3 Lifecycle

#### `POST /v1/appointments/{id}/confirm` (§1.1.3)
Sets `status = CONFIRMED`. Also sets `confirmed_at` (timestamp) and `confirmed_via = "callbot"`. Bumps `version`.
* **Headers:** `Idempotency-Key`, `If-Match`.
* **Errors:** `404 NOT_FOUND`, `409 VERSION_CONFLICT`, `409 INVALID_STATE_TRANSITION`, `422 IDEMPOTENCY_CONFLICT`.

#### `POST /v1/appointments/{id}/cancel` (§1.1.4 / §3.5 SF-05)
Sets `status = CANCELLED`. Persists `cancel_reason` from Appendix A.
* **Headers:** `Idempotency-Key`, `If-Match`.
* **Request body:** `{ "cancel_reason": "PATIENT_UNAVAILABLE", "confirmed": true }`
* **`confirmed: true` is required.** Without it the mock returns `409 CONFIRMATION_REQUIRED` — a single ambiguous turn is not enough to cancel (§3.5 SF-05).
* **Errors:** `404 NOT_FOUND`, `409 CONFIRMATION_REQUIRED`, `409 VERSION_CONFLICT`, `409 INVALID_STATE_TRANSITION`, `422 IDEMPOTENCY_CONFLICT`.

#### `POST /v1/appointments/{id}/transfer` (§1.1.5)
Sets `status = TRANSFERRED`. Persists `transfer_reason` from Appendix A. **No sibling appointment is created** — the transfer is a status flip.
* **Headers:** `Idempotency-Key`, `If-Match`.
* **Request body:** `{ "transfer_reason": "CLINICAL_QUESTION" }`
* **Errors:** `404 NOT_FOUND`, `409 VERSION_CONFLICT`, `409 INVALID_STATE_TRANSITION`, `422 IDEMPOTENCY_CONFLICT`.

#### `POST /v1/appointments/{id}/reschedule` (§1.1.6 / Listing 4)
Atomically books the new slot and releases the old one back into `GET /v1/slots` under its original `slot_id`. Bumps `version`.
* **Headers:** `Idempotency-Key`, `If-Match`.
* **Request body:** `{ "new_slot_id": "slot_91d2", "requested_by": "PATIENT" }`
* **Response `200 OK` — Listing 4 minimal shape** (NOT the full appointment):
  ```json
  {
    "status": "RESCHEDULED",
    "new_slot_id": "slot_91d2",
    "released_slot_id": "slot_77aa",
    "version": 4
  }
  ```
* **Errors:** `404 NOT_FOUND`, `409 SLOT_TAKEN`, `409 VERSION_CONFLICT`, `409 INVALID_STATE_TRANSITION`, `422 IDEMPOTENCY_CONFLICT`.

#### `POST /v1/appointments/{id}/unreachable` (§1.1.7 / §2.2 / §4.2.5)
Marks the appointment UNREACHABLE (no answer / voicemail / line busy). **Idempotent** — each call increments `attempt_count` and re-confirms `UNREACHABLE`, so the harness can record multiple no-answer attempts on the same appointment. Allowed from `{SCHEDULED, BOOKED, UNREACHABLE}`.
* **Headers:** `Idempotency-Key`, `If-Match`.
* **Request body:** `{ "unreachable_reason": "SILENCE" }` — one of `SILENCE`, `VOICEMAIL`, `NO_ANSWER`, `LINE_BUSY` (Appendix A). Body with `attempt_count` is rejected with `400 INVALID_REQUEST` (§4.2.5).
* `Idempotency-Key` is evaluated **before** `If-Match`: a replayed key returns the cached `200` response even if `If-Match` is stale (§4.2.5).
* **Response `200 OK`:** `Appointment`.
* **Errors:** `400 INVALID_REQUEST`, `404 NOT_FOUND`, `409 VERSION_CONFLICT`, `409 INVALID_STATE_TRANSITION`.

### 3.4 Lifecycle State Machine

| From \ Action | confirm | cancel | transfer | reschedule | unreachable |
| :--- | :---: | :---: | :---: | :---: | :---: |
| `SCHEDULED`   | → CONFIRMED | → CANCELLED | → TRANSFERRED | → RESCHEDULED | → UNREACHABLE |
| `BOOKED`      | → CONFIRMED | → CANCELLED | → TRANSFERRED | → RESCHEDULED | → UNREACHABLE |
| `CONFIRMED`   | —           | → CANCELLED | → TRANSFERRED | → RESCHEDULED | — |
| `CANCELLED`   | —           | —           | —             | —             | — |
| `RESCHEDULED` | —           | —           | —             | —             | — |
| `TRANSFERRED` | —           | —           | —             | —             | — |
| `UNREACHABLE` | —           | —           | —             | —             | (idempotent) |

Anything off-script returns `409 INVALID_STATE_TRANSITION`.

## 4. Admin & Operations (`/_harness/*`)

Per-tenant scoring-harness endpoints. **Reserved for the harness** — contract §4.2.4 says the bot MUST NOT call these. Always scoped to the caller's data (plus the canonical contract fixtures).

| Method | Path | Purpose |
| :--- | :--- | :--- |
| `GET` | `/_harness/state` | Full snapshot: patients, slots, appointments. |
| `GET` | `/_harness/patients` | Patients in caller's scope. |
| `GET` | `/_harness/slots` | Slots in caller's scope. |
| `GET` | `/_harness/appointments` | Appointments in caller's scope. |
| `GET` | `/_harness/snapshot` | Capture current state, returns `snapshot_id`. |
| `POST` | `/_harness/snapshot/{sid}/restore` | Reset to a prior snapshot. |
| `POST` | `/_harness/seed` | Reset and seed canonical + per-tenant fixtures. |
| `POST` | `/_harness/reset` | Flush and re-seed. |
| `POST` | `/_harness/time-travel` | Advance the system clock by `seconds` (signed). |

What a seed contains comes from the seed dataset (JSON under `src/clinic_mock/data`), not from code. See the README, "Seed Data". Schedules generate open slots from today for `MOCK_SEED_HORIZON_DAYS`, so a seed on a later day holds later slots. The contract fixtures and the upstream fixed-date rows never move.

## 5. Data Models

### `Patient`

```json
{
  "patient_id": "pt_3391",
  "display_name": "N. V. A.",
  "phone": "0912345600",
  "dob": "1978-03-14",
  "verify": { "full_name": "Nguyễn Văn A", "dob": "1978-03-14" }
}
```

### `PatientRef` (embedded in `Appointment`)

```json
{
  "patient_id": "pt_3391",
  "display_name": "N. V. A.",
  "verify": { "full_name": "Nguyễn Văn A", "dob": "1978-03-14" }
}
```

### `Slot`

```json
{
  "slot_id": "slot_91d2",
  "clinic_id": "cl_vinmec",
  "start_time": "2026-10-14T15:00:00+07:00",
  "end_time": "2026-10-14T15:30:00+07:00",
  "provider_id": "pr_vinmec_1"
}
```

### `Appointment` (Listing 3 canonical shape)

```json
{
  "appointment_id": "apt_00417",
  "status": "SCHEDULED",
  "clinic_id": "cl_vinmec",
  "starts_at": "2026-10-14T15:30:00+07:00",
  "ends_at": "2026-10-14T16:00:00+07:00",
  "department": "Nội tổng quát",
  "patient": { "...": "PatientRef" },
  "cancel_reason": null,
  "transfer_reason": null,
  "unreachable_reason": null,
  "confirmed_at": null,
  "confirmed_via": null,
  "new_slot_id": null,
  "attempt_count": 0,
  "version": 3
}
```

`status` is one of `SCHEDULED`, `BOOKED`, `CONFIRMED`, `CANCELLED`, `RESCHEDULED`, `TRANSFERRED`, `UNREACHABLE` (contract §2.2).

### `AppointmentRescheduleResponse` (Listing 4 minimal)

```json
{
  "status": "RESCHEDULED",
  "new_slot_id": "slot_91d2",
  "released_slot_id": "slot_77aa",
  "version": 4
}
```

### `ErrorBody`

```json
{
  "error": {
    "code": "BAD_KEY",
    "message": "Invalid bearer token.",
    "request_id": "req_5bd34e12d76a",
    "details": []
  }
}
```

## 6. Reason Code Enums (Appendix A)

### Cancel — `cancel_reason`

| Code | When |
| :--- | :--- |
| `PATIENT_UNAVAILABLE` | Patient cannot attend at the scheduled time. |
| `NO_LONGER_NEEDED` | Patient no longer requires the appointment. |
| `WENT_ELSEWHERE` | Patient chose a different provider. |
| `COST` | Price concerns. |
| `UNSPECIFIED` | Catch-all. |

### Transfer — `transfer_reason`

| Code | When |
| :--- | :--- |
| `IDENTITY_FAILED` | §1.1.2 — caller could not verify identity. |
| `PATIENT_NOT_FOUND` | §1.1.9 — no matching patient record on inbound. |
| `OUT_OF_SCOPE` | §1.2.1/1.2.3 — clinical/financial/referral/prescription topic. |
| `CLINICAL_QUESTION` | §1.2.1 — clinical question requiring a human. |
| `NOT_UNDERSTOOD` | §1.1.5 — two consecutive failed understandings. |
| `PATIENT_REQUEST` | Patient asked for a human. |
| `SYSTEM_ERROR` | Bot internal failure. |

### Unreachable — `unreachable_reason`

| Code | When |
| :--- | :--- |
| `SILENCE` | Caller silent 3 turns in a row (scored path via `silence_ms`). |
| `VOICEMAIL` | Voicemail greeting detected (scored path via clip). |
| `NO_ANSWER` | No answer — live call week 6 only. |
| `LINE_BUSY` | Line busy — live call week 6 only. |

## 7. Validation Rules

| Field | Rule |
| :--- | :--- |
| `phone`, `Patient.phone` | VN local 10-digit: `^(02|03|05|07|08|09)\d{8}$` |
| `starts_at`, `ends_at`, `start_time`, `end_time` | RFC 3339 — `Z` or `±HH:MM` offset |
| `dob` | `YYYY-MM-DD` |
| `from`, `to` | RFC 3339. `to > from`, `to - from ≤ 14 days` |
| `date` | `YYYY-MM-DD` |
| `cancel_reason` | One of `PATIENT_UNAVAILABLE`, `NO_LONGER_NEEDED`, `WENT_ELSEWHERE`, `COST`, `UNSPECIFIED` |
| `transfer_reason` | One of `IDENTITY_FAILED`, `PATIENT_NOT_FOUND`, `OUT_OF_SCOPE`, `CLINICAL_QUESTION`, `NOT_UNDERSTOOD`, `PATIENT_REQUEST`, `SYSTEM_ERROR` |
| `unreachable_reason` | One of `SILENCE`, `VOICEMAIL`, `NO_ANSWER`, `LINE_BUSY` |
| `requested_by` (reschedule) | One of `PATIENT`, `STAFF` |
| `If-Match` | Integer ≥ 1 |
| `Idempotency-Key` | ≤ 255 chars |

## 8. Where Things Live

```
src/clinic_mock/
├── main.py            # entry point: `uv run dev` / `uv run prod`
├── app.py             # FastAPI app, middleware (auth, request-id)
├── auth.py            # bearer-key parser; per-key isolation registry
├── config.py          # pydantic-settings (loads .env)
├── errors.py          # ApiError envelope + exception handlers
├── lifecycle.py       # appointment state-transition guards (§2.2)
├── logger.py          # loguru setup
├── routes.py          # all v1 + harness + health routes
├── schemas.py         # Pydantic models matching contract §2.2 + Appendix A
├── dataset.py         # seed dataset: record types, loading, validation, CLI
├── seeding.py         # dataset -> one day's rows (schedules -> open slots)
├── store.py           # in-memory db; seed_default() fills it from the dataset
├── tracing.py         # Langfuse OTel instrumentation
└── data/              # the seed dataset, one JSON file per entity
```

`docs/APIs.md` is the source of truth for the contract; the code is the
source of truth for behavior. The contract at
[callbot-contract-site.vercel.app](https://callbot-contract-site.vercel.app/)
is the source of truth for the contract — where this spec disagrees with it,
the contract wins.
