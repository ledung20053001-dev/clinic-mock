"""The seed dataset: every row the mock starts with, read from JSON instead of code.

One file per entity, in ``data/`` next to this module or in ``MOCK_SEED_DATA_DIR``::

    clinics.json       where care happens
    departments.json   the specialties providers belong to
    providers.json     doctors, each at one clinic in one department
    schedules.json     weekly working hours; open slots are generated from them
    patients.json      people the phone lookup can find
    slots.json         one-off open slots at fixed times
    appointments.json  existing bookings, on a fixed slot or on a generated one

The first three are the catalog and are required. The rest are rows, and a missing
file means no rows of that kind. Every row takes two optional keys:

* ``scope``: ``tenant`` (the default) copies the row once per API key, with the
  key's short hash appended to its id, so keys never see each other's writes.
  ``shared`` seeds a single copy under the canonical tenant, which every key sees.
* ``profile``: ``base`` (the default) is the stock seed and is always loaded.
  A row with any other profile is seeded only when ``MOCK_SEED_PROFILES`` names that
  profile. The callbot demo uses ``demo``.

Any record may carry a ``note``, because JSON has no comments. Unknown keys are
rejected, so a typo fails loudly instead of being silently ignored.

A dataset is checked in full when it loads. Each record's types and formats are
checked first, then the references between files: the clinic and department a
provider names must exist, phones must be unique, and an appointment's patient must
exist and its time must fall on its provider's schedule. Every problem is reported
at once. ``python -m clinic_mock.dataset [DIR]`` runs the same check and prints what
the dataset would seed today.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, datetime
from functools import cached_property, lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from clinic_mock.schemas import (
    AppointmentStatus,
    CancelReason,
    IsoDate,
    IsoDateTime,
    Phone,
    TransferReason,
    UnreachableReason,
)

PACKAGED_DATA_DIR = Path(__file__).parent / "data"
#: The stock seed. Always loaded; other profiles are added on top of it.
BASE_PROFILE = "base"
#: Clinic-local clock of a schedule unless it says otherwise (Vietnam, no DST).
CLINIC_UTC_OFFSET = "+07:00"

Scope = Literal["tenant", "shared"]
ClockTime = Annotated[str, StringConstraints(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
UtcOffset = Annotated[str, StringConstraints(pattern=r"^(Z|[+-](0\d|1[0-4]):[0-5]\d)$")]
ProfileName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]*$")]
NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def minutes(clock: str) -> int:
    """Minutes since midnight of an ``HH:MM`` clock time."""

    hours, mins = clock.split(":")
    return int(hours) * 60 + int(mins)


class DatasetError(ValueError):
    """The seed dataset is invalid. Lists every problem found, not just the first."""

    def __init__(self, source: Path, problems: list[str]) -> None:
        self.source = source
        self.problems = problems
        listed = "\n".join(f"  - {problem}" for problem in problems)
        super().__init__(f"Invalid seed dataset in {source}:\n{listed}")


# ----- records ---------------------------------------------------------------------


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    note: str | None = None


class Row(Record):
    scope: Scope = "tenant"
    profile: ProfileName = BASE_PROFILE


class Clinic(Record):
    id: NonBlank
    name: NonBlank
    city: NonBlank
    district: str | None = None


class Department(Record):
    code: NonBlank
    # The value slots and appointments carry, spoken by the bot as "khoa <name>".
    name: NonBlank


class Provider(Record):
    id: NonBlank
    name: NonBlank
    clinic_id: str
    department: str

    def metadata(self) -> dict[str, str]:
        return {
            "id": self.id,
            "name": self.name,
            "clinic_id": self.clinic_id,
            "department": self.department,
        }


class Schedule(Row):
    """A provider's weekly hours, cut into open slots of ``slot_minutes``.

    A schedule with both ``valid_from`` and ``valid_to`` is a fixed block and is
    generated in full, whatever today's date is. Any other schedule rolls with the
    calendar: it starts today, or at ``valid_from`` if that is later, runs for
    ``MOCK_SEED_HORIZON_DAYS``, and never goes past ``valid_to``. Session times are
    on the ``utc_offset`` clock.
    """

    provider_id: str
    weekdays: tuple[Annotated[int, Field(ge=0, le=6)], ...] = Field(
        min_length=1
    )  # 0 = Monday
    sessions: tuple[tuple[ClockTime, ClockTime], ...] = Field(min_length=1)
    slot_minutes: int = Field(default=30, ge=5, le=240)
    utc_offset: UtcOffset = CLINIC_UTC_OFFSET
    valid_from: date | None = None
    valid_to: date | None = None
    slot_id_format: str = "s_{provider}_{date}_{time}"

    @model_validator(mode="after")
    def _consistent(self) -> Schedule:
        previous_end = -1
        for start, end in sorted(self.sessions):
            length = minutes(end) - minutes(start)
            if length <= 0:
                raise ValueError(f"session {start}-{end} does not end after it starts")
            if length % self.slot_minutes:
                raise ValueError(
                    f"session {start}-{end} is not a whole number of "
                    f"{self.slot_minutes}-minute slots"
                )
            if minutes(start) < previous_end:
                raise ValueError(f"session {start}-{end} overlaps the one before it")
            previous_end = minutes(end)
        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            raise ValueError("valid_to is before valid_from")
        try:
            self.slot_id_format.format(provider="p", date="20260101", time="0800")
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError(
                "slot_id_format may only use {provider}, {date} and {time}"
            ) from exc
        if "{date}" not in self.slot_id_format or "{time}" not in self.slot_id_format:
            raise ValueError(
                "slot_id_format needs {date} and {time}, or ids would repeat"
            )
        return self

    @property
    def is_fixed(self) -> bool:
        return self.valid_from is not None and self.valid_to is not None

    def clock_times(self) -> Iterator[str]:
        """The start of every slot in one working day, as ``HH:MM``."""

        for start, end in self.sessions:
            for minute in range(minutes(start), minutes(end), self.slot_minutes):
                yield f"{minute // 60:02d}:{minute % 60:02d}"


class Identity(BaseModel):
    """What the bot checks the caller against: the spoken name and date of birth."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    full_name: NonBlank
    dob: IsoDate


class Patient(Row):
    id: NonBlank
    display_name: NonBlank
    phone: Phone
    dob: IsoDate
    address: str | None = Field(default=None, max_length=255)
    verify: Identity

    @model_validator(mode="after")
    def _consistent(self) -> Patient:
        date.fromisoformat(self.dob)  # the pattern alone accepts 1990-13-45
        if self.verify.dob != self.dob:
            raise ValueError("verify.dob must equal dob")
        return self


class Slot(Row):
    slot_id: NonBlank
    clinic_id: str
    provider_id: str
    start_time: IsoDateTime
    end_time: IsoDateTime

    @model_validator(mode="after")
    def _consistent(self) -> Slot:
        if datetime.fromisoformat(self.end_time) <= datetime.fromisoformat(
            self.start_time
        ):
            raise ValueError("end_time must be after start_time")
        return self


class Appointment(Row):
    """An existing booking, on a fixed slot or on a generated one.

    Fixed: ``slot_id``, ``starts_at`` and ``ends_at``, as in the contract fixtures.
    The slot does not have to be open. apt_00417 holds slot_77aa, which is listed
    only after a cancel or reschedule frees it.

    Generated: ``working_day`` and ``time``. The booking takes the provider's slot at
    that clock time on the n-th upcoming day the provider has one (1 = the first day
    after today), so a call list stays near-term whatever the date. A cancelled
    booking leaves its slot open, as ``POST /cancel`` does.
    """

    appointment_id: NonBlank
    patient_id: str
    provider_id: str
    status: AppointmentStatus
    working_day: int | None = Field(default=None, ge=1)
    time: ClockTime | None = None
    slot_id: str | None = None
    starts_at: IsoDateTime | None = None
    ends_at: IsoDateTime | None = None
    # Both default to the provider's. The contract's apt_00417 keeps a department
    # its provider is not in, so an explicit value wins.
    clinic_id: str | None = None
    department: str | None = None
    cancel_reason: CancelReason | None = None
    transfer_reason: TransferReason | None = None
    unreachable_reason: UnreachableReason | None = None
    confirmed_via: str | None = None
    attempt_count: int = Field(default=0, ge=0)
    version: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _consistent(self) -> Appointment:
        generated = (self.working_day, self.time)
        fixed = (self.slot_id, self.starts_at, self.ends_at)
        if any(v is not None for v in generated) == any(v is not None for v in fixed):
            raise ValueError(
                "give working_day and time, or slot_id, starts_at and ends_at"
            )
        if None in (generated if self.working_day or self.time else fixed):
            raise ValueError(
                "working_day and time, or slot_id, starts_at and ends_at, go together"
            )
        if self.starts_at and self.ends_at:
            if datetime.fromisoformat(self.ends_at) <= datetime.fromisoformat(
                self.starts_at
            ):
                raise ValueError("ends_at must be after starts_at")
        for status, reason, name in (
            ("CANCELLED", self.cancel_reason, "cancel_reason"),
            ("TRANSFERRED", self.transfer_reason, "transfer_reason"),
            ("UNREACHABLE", self.unreachable_reason, "unreachable_reason"),
        ):
            if (self.status == status) != (reason is not None):
                raise ValueError(f"{name} is set exactly when status is {status}")
        if self.status == "UNREACHABLE" and self.attempt_count < 1:
            raise ValueError("an UNREACHABLE appointment has at least one attempt")
        return self

    @property
    def is_generated(self) -> bool:
        return self.working_day is not None

    @property
    def holds_slot(self) -> bool:
        return self.status != "CANCELLED"


# ----- the dataset -----------------------------------------------------------------


@dataclass(frozen=True, eq=False)  # hashed by identity: loads are cached, plans too
class Dataset:
    source: Path
    clinics: tuple[Clinic, ...]
    departments: tuple[Department, ...]
    providers: tuple[Provider, ...]
    schedules: tuple[Schedule, ...] = ()
    patients: tuple[Patient, ...] = ()
    slots: tuple[Slot, ...] = ()
    appointments: tuple[Appointment, ...] = ()

    @cached_property
    def _providers(self) -> dict[str, Provider]:
        return {provider.id: provider for provider in self.providers}

    def provider(self, provider_id: str) -> Provider:
        try:
            return self._providers[provider_id]
        except KeyError:
            raise KeyError(f"unknown provider {provider_id!r}") from None

    @cached_property
    def profiles(self) -> frozenset[str]:
        rows: Iterable[Row] = (
            *self.schedules,
            *self.patients,
            *self.slots,
            *self.appointments,
        )
        return frozenset({BASE_PROFILE, *(row.profile for row in rows)})


_FILES: dict[str, tuple[type[Record], bool]] = {
    # name: (record type, required)
    "clinics": (Clinic, True),
    "departments": (Department, True),
    "providers": (Provider, True),
    "schedules": (Schedule, False),
    "patients": (Patient, False),
    "slots": (Slot, False),
    "appointments": (Appointment, False),
}


def load_dataset(directory: str | Path | None = None) -> Dataset:
    """The dataset in ``directory`` (default: the packaged one), validated.

    Cached per directory: the files are read once per process, however often the
    store is reseeded.
    """

    source = Path(directory).expanduser().resolve() if directory else PACKAGED_DATA_DIR
    return _load(source)


@lru_cache(maxsize=8)
def _load(source: Path) -> Dataset:
    problems: list[str] = []
    if not source.is_dir():
        raise DatasetError(source, ["not a directory"])
    tables = {
        name: tuple(_read(source / f"{name}.json", model, required, problems))
        for name, (model, required) in _FILES.items()
    }
    if problems:
        raise DatasetError(source, problems)
    dataset = Dataset(source=source, **tables)  # type: ignore[arg-type]
    problems = _cross_check(dataset)
    if problems:
        raise DatasetError(source, problems)
    return dataset


def _read(
    path: Path, model: type[Record], required: bool, problems: list[str]
) -> list[Any]:
    if not path.is_file():
        if required:
            problems.append(f"{path.name}: missing")
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        problems.append(f"{path.name}: not readable JSON ({exc})")
        return []
    if not isinstance(raw, list):
        problems.append(f"{path.name}: must hold a JSON array of records")
        return []
    records = []
    for index, item in enumerate(raw):
        try:
            records.append(model.model_validate(item))
        except ValidationError as exc:
            label = _label(item, index)
            for error in exc.errors():
                where = ".".join(str(part) for part in error["loc"]) or "record"
                problems.append(f"{path.name}[{label}] {where}: {error['msg']}")
    return records


def _label(item: Any, index: int) -> str:
    if isinstance(item, dict):
        for key in ("id", "appointment_id", "slot_id", "code", "provider_id"):
            if isinstance(item.get(key), str):
                return item[key]
    return f"#{index}"


def _cross_check(d: Dataset) -> list[str]:
    """References between files. Record-level formats were checked on read."""

    problems: list[str] = []

    def unique(what: str, values: Iterable[str]) -> None:
        for value, count in Counter(values).items():
            if count > 1:
                problems.append(f"{what} {value!r} appears {count} times")

    unique("clinic id", (c.id for c in d.clinics))
    unique("department code", (x.code for x in d.departments))
    unique("department name", (x.name for x in d.departments))
    unique("provider id", (p.id for p in d.providers))
    unique("patient id", (p.id for p in d.patients))
    unique("slot id", (s.slot_id for s in d.slots))
    unique("appointment id", (a.appointment_id for a in d.appointments))
    # A key sees the shared patients beside its own copies, so a phone must be
    # unique across both, or the inbound lookup finds two records.
    unique("patient phone", (p.phone for p in d.patients))

    clinics = {c.id for c in d.clinics}
    departments = {x.name for x in d.departments}
    providers = {p.id: p for p in d.providers}
    patients = {p.id: p for p in d.patients}

    for p in d.providers:
        if p.clinic_id not in clinics:
            problems.append(f"provider {p.id}: unknown clinic {p.clinic_id!r}")
        if p.department not in departments:
            problems.append(f"provider {p.id}: unknown department {p.department!r}")
    for s in d.schedules:
        if s.provider_id not in providers:
            problems.append(f"schedule of {s.provider_id!r}: unknown provider")
    for slot in d.slots:
        owner = providers.get(slot.provider_id)
        if owner is None:
            problems.append(
                f"slot {slot.slot_id}: unknown provider {slot.provider_id!r}"
            )
        elif slot.clinic_id != owner.clinic_id:
            problems.append(
                f"slot {slot.slot_id}: clinic {slot.clinic_id!r} is not where "
                f"{owner.id} works ({owner.clinic_id!r})"
            )

    taken: Counter[tuple[str, str, int | None, str | None]] = Counter()
    for a in d.appointments:
        label = f"appointment {a.appointment_id}"
        patient = patients.get(a.patient_id)
        if patient is None:
            problems.append(f"{label}: unknown patient {a.patient_id!r}")
        elif a.scope == "shared" and patient.scope != "shared":
            problems.append(
                f"{label}: is shared, but patient {a.patient_id} is copied per key"
            )
        elif patient.profile not in (BASE_PROFILE, a.profile):
            problems.append(
                f"{label}: patient {a.patient_id} is only seeded with profile "
                f"{patient.profile!r}"
            )
        owner = providers.get(a.provider_id)
        if owner is None:
            problems.append(f"{label}: unknown provider {a.provider_id!r}")
            continue
        if a.clinic_id is not None and a.clinic_id != owner.clinic_id:
            problems.append(
                f"{label}: clinic {a.clinic_id!r} is not where {owner.id} works "
                f"({owner.clinic_id!r})"
            )
        if a.department is not None and a.department not in departments:
            problems.append(f"{label}: unknown department {a.department!r}")
        if a.is_generated:
            taken[(a.provider_id, a.scope, a.working_day, a.time)] += 1
            if not any(offers(s, a) for s in d.schedules):
                problems.append(
                    f"{label}: {owner.id} has no rolling {a.scope} schedule in profile "
                    f"{BASE_PROFILE!r} or {a.profile!r} with a slot at {a.time}"
                )
    for (provider_id, _, day, at), count in taken.items():
        if count > 1:
            problems.append(
                f"{count} appointments take {provider_id}'s {at} slot "
                f"on working day {day}"
            )
    return problems


def offers(schedule: Schedule, appointment: Appointment) -> bool:
    """Whether ``schedule`` generates the slot a generated appointment asks for."""

    return (
        schedule.provider_id == appointment.provider_id
        and not schedule.is_fixed
        and schedule.scope == appointment.scope
        and schedule.profile in (BASE_PROFILE, appointment.profile)
        and appointment.time in set(schedule.clock_times())
    )


# ----- CLI -------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    from clinic_mock.config import MockSeedSettings
    from clinic_mock.seeding import build_plan, clinic_today, parse_profiles

    config = MockSeedSettings()
    parser = argparse.ArgumentParser(
        prog="python -m clinic_mock.dataset",
        description="Validate a seed dataset and show what it seeds today.",
    )
    parser.add_argument(
        "directory",
        nargs="?",
        help="dataset directory (default: MOCK_SEED_DATA_DIR, else the packaged one)",
    )
    parser.add_argument("--profiles", default=config.PROFILES, help="comma-separated")
    parser.add_argument("--horizon-days", type=int, default=config.HORIZON_DAYS)
    args = parser.parse_args(argv)

    today = clinic_today()
    try:
        dataset = load_dataset(args.directory or config.DATA_DIR or None)
        plan = build_plan(
            dataset, parse_profiles(args.profiles), today, args.horizon_days
        )
    except DatasetError as exc:
        print(exc, file=sys.stderr)
        return 1

    print(
        f"{dataset.source}: {len(dataset.clinics)} clinics, "
        f"{len(dataset.departments)} departments, {len(dataset.providers)} providers; "
        f"profiles available: {', '.join(sorted(dataset.profiles))}"
    )
    print(
        f"Seeding {', '.join(sorted(plan.profiles))} on {today} "
        f"over {args.horizon_days} days "
        "(tenant rows are copied once per API key):"
    )
    for scope in ("shared", "tenant"):
        print(
            f"  {scope:<7} {sum(p.scope == scope for p in plan.patients):>5} patients"
            f" {sum(s.scope == scope for s in plan.slots):>6} open slots"
            f" {sum(a.record.scope == scope for a in plan.appointments):>4}"
            " appointments"
        )
    print(f"\n  {'clinic':<34}{'providers':>10}{'open slots':>12}{'appointments':>14}")
    for clinic in dataset.clinics:
        print(
            f"  {clinic.name:<34}"
            f"{sum(p.clinic_id == clinic.id for p in dataset.providers):>10}"
            f"{sum(s.clinic_id == clinic.id for s in plan.slots):>12}"
            f"{sum(a.clinic_id == clinic.id for a in plan.appointments):>14}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
