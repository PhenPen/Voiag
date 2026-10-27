"""Tests for the booking rules. No network, no API key needed."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app import tools
from app.store import Store

CONFIG = {
    "business": {
        "name": "Test Studio",
        "timezone": "UTC",
        "address": "1 Test Road",
        "phone": "+44 20 0000 0000",
    },
    "hours": {"open": "09:00", "close": "17:00", "lunch": ["12:30", "13:30"], "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]},
    "services": [
        {"id": "consult", "name": "consultation", "minutes": 30},
        {"id": "session", "name": "full session", "minutes": 60},
    ],
    "slot_minutes": 30,
    "max_days_ahead": 30,
}


def next_weekday(index: int) -> str:
    """Next date matching weekday index (Mon=0), never today."""
    today = datetime.now(ZoneInfo("UTC")).date()
    delta = (index - today.weekday()) % 7 or 7
    return (today + timedelta(days=delta)).isoformat()


class BookingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "test.db")
        self.business = tools.Business(CONFIG)
        self.monday = next_weekday(0)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_declarations_build(self) -> None:
        declared = tools.declarations(self.business)
        self.assertTrue(declared)
        names = [fn.name for fn in declared[0].function_declarations]
        self.assertIn("book_appointment", names)
        self.assertIn("check_availability", names)

    def test_availability_on_a_weekday(self) -> None:
        result = tools.execute(
            "check_availability", {"date_from": self.monday, "service": "consult"}, self.business, self.store
        )
        self.assertTrue(result["ok"], result)
        self.assertGreater(len(result["slots"]), 0)
        self.assertLessEqual(len(result["slots"]), tools.MAX_SLOTS_RETURNED)

    def test_closed_at_weekend(self) -> None:
        sunday = next_weekday(6)
        result = tools.execute(
            "check_availability", {"date_from": sunday, "service": "consult"}, self.business, self.store
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["slots"], [])

    def test_booking_a_returned_slot(self) -> None:
        slots = tools.execute(
            "check_availability", {"date_from": self.monday, "service": "consult"}, self.business, self.store
        )["slots"]

        booked = tools.execute(
            "book_appointment",
            {"service": "consult", "start": slots[0]["start"], "name": "Ada", "phone": "0700"},
            self.business,
            self.store,
        )
        self.assertTrue(booked["ok"], booked)

        found = tools.execute("find_appointments", {"phone": "0700"}, self.business, self.store)
        self.assertEqual(len(found["appointments"]), 1)

    def test_double_booking_is_refused(self) -> None:
        start = tools.execute(
            "check_availability", {"date_from": self.monday, "service": "consult"}, self.business, self.store
        )["slots"][0]["start"]

        first = tools.execute(
            "book_appointment", {"service": "consult", "start": start, "name": "Ada", "phone": "0700"}, self.business, self.store
        )
        second = tools.execute(
            "book_appointment", {"service": "consult", "start": start, "name": "Bob", "phone": "0701"}, self.business, self.store
        )

        self.assertTrue(first["ok"], first)
        self.assertFalse(second["ok"], second)

    def test_double_booking_refused_outside_utc(self) -> None:
        """Stored times are UTC; offered times carry a local offset."""
        config = json.loads(json.dumps(CONFIG))
        config["business"]["timezone"] = "Europe/Paris"
        business = tools.Business(config)

        start = tools.execute(
            "check_availability", {"date_from": self.monday, "service": "consult"}, business, self.store
        )["slots"][0]["start"]
        self.assertNotIn("+00:00", start)

        first = tools.execute(
            "book_appointment", {"service": "consult", "start": start, "name": "Ada", "phone": "0700"}, business, self.store
        )
        second = tools.execute(
            "book_appointment", {"service": "consult", "start": start, "name": "Bob", "phone": "0701"}, business, self.store
        )

        self.assertTrue(first["ok"], first)
        self.assertFalse(second["ok"], second)

        remaining = tools.execute(
            "check_availability", {"date_from": self.monday, "service": "consult"}, business, self.store
        )["slots"]
        self.assertNotIn(start, [slot["start"] for slot in remaining])

    def test_off_grid_time_is_refused(self) -> None:
        start = tools.execute(
            "check_availability", {"date_from": self.monday, "service": "consult"}, self.business, self.store
        )["slots"][0]["start"]

        moment = datetime.fromisoformat(start) + timedelta(minutes=7)
        result = tools.execute(
            "book_appointment",
            {"service": "consult", "start": moment.isoformat(), "name": "Ada", "phone": "0700"},
            self.business,
            self.store,
        )
        self.assertFalse(result["ok"], result)

    def test_cancel_requires_matching_phone(self) -> None:
        start = tools.execute(
            "check_availability", {"date_from": self.monday, "service": "consult"}, self.business, self.store
        )["slots"][0]["start"]

        booked = tools.execute(
            "book_appointment", {"service": "consult", "start": start, "name": "Ada", "phone": "0700"}, self.business, self.store
        )
        appointment_id = booked["appointment_id"]

        wrong = tools.execute(
            "cancel_appointment", {"appointment_id": appointment_id, "phone": "9999"}, self.business, self.store
        )
        right = tools.execute(
            "cancel_appointment", {"appointment_id": appointment_id, "phone": "0700"}, self.business, self.store
        )

        self.assertFalse(wrong["ok"], wrong)
        self.assertTrue(right["ok"], right)

    def test_prompt_has_business_and_time(self) -> None:
        prompt = self.business.system_prompt()
        self.assertIn("Test Studio", prompt)
        self.assertIn("check_availability", prompt)


if __name__ == "__main__":
    unittest.main()
