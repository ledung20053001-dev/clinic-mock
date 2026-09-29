"""Turn the seed dataset into the rows of one day: the plan the store is filled from.

Nothing here touches the store. A plan depends only on the dataset, the profiles,
the date and the horizon, so it is computed once for each combination and replayed on
every reset, which both test suites do before each test.

How open slots come about:

1. One-off slots (``slots.json``) and fixed appointments hold their times.
2. Schedules generate slots for their window. A generated slot that overlaps a
   held time of the same provider is dropped, because the explicit row wins. That is
   how the contract's slot_91d2 and apt_00417 stay the only bookings at their times
   when the demo profile gives the same doctor a schedule over the same day.
3. Two generated slots of one provider must never overlap: that is a dataset error.
4. Each appointment takes its slot out of the open set, unless it is cancelled.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from typing import TypeVar

from clinic_mock.dataset import (
    BASE_PROFILE,
    Appointment,
    Dataset,
    DatasetError,
    Patient,
    Row,
    Schedule,
    Scope,
    Slot,
    minutes,
)

R = TypeVar("R", bound=Row)

CLINIC_TZ = timezone(timedelta(hours=7), name="Asia/Ho_Chi_Minh")


def clinic_today() -> date:
    """Today on the clinics' clock, which rolling schedules count from."""

    return datetime.now(CLINIC_TZ).date()


def parse_profiles(raw: str) -> frozenset[str]:
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class PlannedSlot:
    slot_id: str  # before the per-key suffix
    scope: Scope
    clinic_id: str
    provider_id: str
    provider_name: str
    department: str
    start_time: str
    end_time: str
    starts: datetime
    ends: datetime
    profile: str
    # Set for generated slots only: the day and clock time the schedule made it for,
    # and whether that schedule rolls (only a rolling one can carry a call list).
    day: date | None = None
    clock: str | None = None
    rolling: bool = False


@dataclass(frozen=True)
class PlannedAppointment:
    record: Appointment
    patient_scope: Scope
    slot_id: str  # before the per-key suffix
    clinic_id: str
    provider_name: str
    department: str
    starts_at: str
    ends_at: str


@dataclass(
    frozen=True, eq=False
)  # hashed by identity: plans are cached, rows per plan too
class SeedPlan:
    profiles: frozenset[str]
    patients: tuple[Patient, ...]
    slots: tuple[PlannedSlot, ...]  # open slots, earliest first
    appointments: tuple[PlannedAppointment, ...]


@lru_cache(maxsize=16)
def build_plan(
    dataset: Dataset, profiles: frozenset[str], today: date, horizon_days: int
) -> SeedPlan:
    """What to seed on ``today``; :class:`DatasetError` if it cannot be seeded."""

    unknown = profiles - dataset.profiles
    if unknown:
        raise DatasetError(
            dataset.source,
            [
                f"profile {name!r} is requested but no row uses it "
                f"(available: {', '.join(sorted(dataset.profiles))})"
                for name in sorted(unknown)
            ],
        )
    profiles = profiles | {BASE_PROFILE}
    appointments = _active(dataset.appointments, profiles)
    fixed = [_one_off(dataset, slot) for slot in _active(dataset.slots, profiles)]
    held: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
    for slot in fixed:
        held[slot.provider_id].append((slot.starts, slot.ends))
    for appointment in appointments:
        if not appointment.is_generated:
            held[appointment.provider_id].append(
                (
                    datetime.fromisoformat(str(appointment.starts_at)),
                    datetime.fromisoformat(str(appointment.ends_at)),
                )
            )

    generated = [
        slot
        for schedule in _active(dataset.schedules, profiles)
        for slot in _generate(dataset, schedule, today, horizon_days)
        if not any(
            slot.starts < end and start < slot.ends
            for start, end in held[slot.provider_id]
        )
    ]
    problems = _double_bookings(generated)

    open_slots: dict[tuple[Scope, str], PlannedSlot] = {}
    for slot in sorted([*fixed, *generated], key=lambda s: (s.starts, s.slot_id)):
        key = (slot.scope, slot.slot_id)
        if key in open_slots:
            problems.append(
                f"slot id {slot.slot_id!r} is produced twice in scope {slot.scope}"
            )
        open_slots[key] = slot

    patients = {p.id: p for p in _active(dataset.patients, profiles)}
    planned: list[PlannedAppointment] = []
    for appointment in appointments:
        provider = dataset.provider(appointment.provider_id)
        patient = patients[appointment.patient_id]
        if appointment.is_generated:
            slot = _nth_slot(appointment, generated, today)
            if slot is None:
                problems.append(
                    f"appointment {appointment.appointment_id}: {provider.id} has "
                    f"fewer than {appointment.working_day} upcoming "
                    f"{appointment.time} slots within "
                    f"{horizon_days} days; raise MOCK_SEED_HORIZON_DAYS"
                )
                continue
            slot_id, starts_at, ends_at = slot.slot_id, slot.start_time, slot.end_time
        else:
            slot_id = str(appointment.slot_id)
            starts_at, ends_at = str(appointment.starts_at), str(appointment.ends_at)
        if appointment.holds_slot:
            open_slots.pop((appointment.scope, slot_id), None)
        planned.append(
            PlannedAppointment(
                record=appointment,
                patient_scope=patient.scope,
                slot_id=slot_id,
                clinic_id=appointment.clinic_id or provider.clinic_id,
                provider_name=provider.name,
                department=appointment.department or provider.department,
                starts_at=starts_at,
                ends_at=ends_at,
            )
        )

    if problems:
        raise DatasetError(dataset.source, problems)
    return SeedPlan(
        profiles=profiles,
        patients=tuple(patients.values()),
        slots=tuple(open_slots.values()),
        appointments=tuple(planned),
    )


def _active(rows: Iterable[R], profiles: frozenset[str]) -> list[R]:
    return [row for row in rows if row.profile in profiles]


def _one_off(dataset: Dataset, slot: Slot) -> PlannedSlot:
    provider = dataset.provider(slot.provider_id)
    return PlannedSlot(
        slot_id=slot.slot_id,
        scope=slot.scope,
        clinic_id=slot.clinic_id,
        provider_id=provider.id,
        provider_name=provider.name,
        department=provider.department,
        start_time=slot.start_time,
        end_time=slot.end_time,
        starts=datetime.fromisoformat(slot.start_time),
        ends=datetime.fromisoformat(slot.end_time),
        profile=slot.profile,
    )


def _window(schedule: Schedule, today: date, horizon_days: int) -> tuple[date, date]:
    """First and last day (inclusive) a schedule generates slots for."""

    if schedule.valid_from is not None and schedule.valid_to is not None:
        return schedule.valid_from, schedule.valid_to
    first = max(today, schedule.valid_from or today)
    last = today + timedelta(days=horizon_days - 1)
    if schedule.valid_to is not None:
        last = min(last, schedule.valid_to)
    return first, last


def _offset(utc_offset: str) -> timezone:
    if utc_offset == "Z":
        return timezone.utc
    sign = -1 if utc_offset[0] == "-" else 1
    return timezone(sign * timedelta(minutes=minutes(utc_offset[1:])))


def _generate(
    dataset: Dataset, schedule: Schedule, today: date, horizon_days: int
) -> Iterator[PlannedSlot]:
    provider = dataset.provider(schedule.provider_id)
    first, last = _window(schedule, today, horizon_days)
    tz = _offset(schedule.utc_offset)
    clocks = list(schedule.clock_times())
    weekdays = set(schedule.weekdays)
    length = timedelta(minutes=schedule.slot_minutes)
    day = first
    while day <= last:
        if day.weekday() in weekdays:
            for clock in clocks:
                hour, minute = divmod(minutes(clock), 60)
                starts = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
                ends = starts + length
                yield PlannedSlot(
                    slot_id=schedule.slot_id_format.format(
                        provider=provider.id,
                        date=f"{day:%Y%m%d}",
                        time=clock.replace(":", ""),
                    ),
                    scope=schedule.scope,
                    clinic_id=provider.clinic_id,
                    provider_id=provider.id,
                    provider_name=provider.name,
                    department=provider.department,
                    # Written out, not isoformat(): a "Z" schedule keeps its Z.
                    start_time=f"{starts:%Y-%m-%dT%H:%M:%S}{schedule.utc_offset}",
                    end_time=f"{ends:%Y-%m-%dT%H:%M:%S}{schedule.utc_offset}",
                    starts=starts,
                    ends=ends,
                    profile=schedule.profile,
                    day=day,
                    clock=clock,
                    rolling=not schedule.is_fixed,
                )
        day += timedelta(days=1)


def _double_bookings(slots: list[PlannedSlot]) -> list[str]:
    by_provider: dict[str, list[PlannedSlot]] = defaultdict(list)
    for slot in slots:
        by_provider[slot.provider_id].append(slot)
    problems = []
    for provider_id, own in by_provider.items():
        own.sort(key=lambda s: s.starts)
        for before, after in zip(own, own[1:], strict=False):
            if after.starts < before.ends:
                problems.append(
                    f"provider {provider_id} is double-booked: schedules give both "
                    f"{before.slot_id} and {after.slot_id}"
                )
                break
    return problems


def _nth_slot(
    appointment: Appointment, generated: list[PlannedSlot], today: date
) -> PlannedSlot | None:
    """The slot a generated appointment takes: its clock time, n-th upcoming day.

    Counted over every generated slot, taken or not, so one appointment never moves
    another; the dataset check guarantees no two ask for the same one.
    """

    days = sorted(
        (slot for slot in generated if _matches(slot, appointment, today)),
        key=lambda slot: slot.starts,
    )
    index = int(appointment.working_day or 0) - 1
    return days[index] if index < len(days) else None


def _matches(slot: PlannedSlot, appointment: Appointment, today: date) -> bool:
    return (
        slot.rolling
        and slot.provider_id == appointment.provider_id
        and slot.scope == appointment.scope
        and slot.profile in (BASE_PROFILE, appointment.profile)
        and slot.clock == appointment.time
        and slot.day is not None
        and slot.day > today
    )
