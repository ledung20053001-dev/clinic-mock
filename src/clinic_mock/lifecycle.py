"""Appointment state-machine helpers per contract §2.2."""

from __future__ import annotations

from clinic_mock.errors import conflict
from clinic_mock.schemas import AppointmentStatus

APPOINTMENT_TRANSITIONS = {
    "confirm": {"SCHEDULED", "BOOKED", "CONFIRMED", "RESCHEDULED"},
    "cancel": {"SCHEDULED", "BOOKED", "CONFIRMED", "RESCHEDULED"},
    "transfer": {"SCHEDULED", "BOOKED", "CONFIRMED"},
    "reschedule": {"SCHEDULED", "BOOKED", "CONFIRMED", "RESCHEDULED"},
    # §2.2 — UNREACHABLE set when no-answer / voicemail / line busy; bumps
    # attempt_count via the route.
    "unreachable": {"SCHEDULED", "BOOKED"},
}


def assert_appointment_transition(current: AppointmentStatus, action: str) -> None:
    allowed = APPOINTMENT_TRANSITIONS[action]
    if current not in allowed:
        raise conflict(
            "INVALID_STATE_TRANSITION",
            f"Cannot {action} an appointment in status {current}.",
        )
