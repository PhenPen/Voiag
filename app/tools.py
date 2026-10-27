"""Business rules exposed to the model as callable tools.

The model never computes time on its own. Every slot it can offer came from
`available_slots`, and every booking is re-validated here before it hits the DB.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from google.genai import types

from .store import SlotTaken, Store

DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MAX_SLOTS_RETURNED = 8


class Business:
    def __init__(self, config: dict):
        business = config["business"]
        hours = config["hours"]

        self.name = business["name"]
        self.address = business.get("address", "")
        self.phone = business.get("phone", "")
        self.timezone_name = business.get("timezone", "UTC")
        self.tz = ZoneInfo(self.timezone_name)

        self.services = {service["id"]: service for service in config["services"]}
        self.open_time = time.fromisoformat(hours["open"])
        self.close_time = time.fromisoformat(hours["close"])
        self.lunch = [time.fromisoformat(value) for value in hours.get("lunch", [])]
        self.open_days = {DAY_KEYS.index(day.lower()[:3]) for day in hours["days"]}
        self.slot_minutes = int(config.get("slot_minutes", 30))
        self.max_days_ahead = int(config.get("max_days_ahead", 30))

    # -- helpers -----------------------------------------------------------

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def label(self, moment: datetime) -> str:
        """Human phrase the model can read out loud: 'Tuesday 28 October at 15:00'."""
        local = moment.astimezone(self.tz)
        return f"{local:%A} {local.day} {local:%B} at {local:%H:%M}"

    def service_name(self, service_id: str) -> str:
        return self.services.get(service_id, {}).get("name", service_id)

    def duration_minutes(self, service_id: str) -> int:
        return int(self.services.get(service_id, {}).get("minutes", self.slot_minutes))

    def slots_for_day(self, day: date, minutes: int) -> list[datetime]:
        """Every bookable start time on one day, ignoring existing bookings."""
        if day.weekday() not in self.open_days:
            return []

        start = datetime.combine(day, self.open_time, tzinfo=self.tz)
        close = datetime.combine(day, self.close_time, tzinfo=self.tz)
        length = timedelta(minutes=minutes)
        step = timedelta(minutes=self.slot_minutes)
        now = self.now()

        slots = []
        current = start
        while current + length <= close:
            if current > now and not self._hits_lunch(current, current + length):
                slots.append(current)
            current += step
        return slots

    def _hits_lunch(self, start: datetime, end: datetime) -> bool:
        if len(self.lunch) != 2:
            return False
        lunch_start = datetime.combine(start.date(), self.lunch[0], tzinfo=self.tz)
        lunch_end = datetime.combine(start.date(), self.lunch[1], tzinfo=self.tz)
        return start < lunch_end and end > lunch_start

    def find_slot(self, moment: datetime, minutes: int) -> datetime | None:
        for slot in self.slots_for_day(moment.astimezone(self.tz).date(), minutes):
            if slot == moment:
                return slot
        return None

    def available_slots(
        self, store: Store, first_day: date, last_day: date, service_id: str
    ) -> list[datetime]:
        minutes = self.duration_minutes(service_id)
        today = self.now().date()
        first_day = max(first_day, today)
        last_day = min(last_day, today + timedelta(days=self.max_days_ahead))

        found: list[datetime] = []
        day = first_day
        while day <= last_day and len(found) < MAX_SLOTS_RETURNED:
            for slot in self.slots_for_day(day, minutes):
                end = slot + timedelta(minutes=minutes)
                if not store.has_overlap(slot, end):
                    found.append(slot)
                    if len(found) >= MAX_SLOTS_RETURNED:
                        break
            day += timedelta(days=1)
        return found

    def info_text(self) -> str:
        days = ", ".join(day.capitalize() for day in DAY_KEYS if DAY_KEYS.index(day) in self.open_days)
        services = "; ".join(
            f"{service['name']} ({service['minutes']} minutes)" for service in self.services.values()
        )
        lunch = ""
        if len(self.lunch) == 2:
            lunch = f", closed {self.lunch[0]:%H:%M} to {self.lunch[1]:%H:%M}"
        return (
            f"{self.name}. {self.address}. Phone {self.phone}. "
            f"Open {days}, {self.open_time:%H:%M} to {self.close_time:%H:%M}{lunch}. "
            f"Services: {services}. Timezone: {self.timezone_name}."
        )

    def system_prompt(self) -> str:
        now = self.now()
        service_list = ", ".join(service["id"] for service in self.services.values())
        return f"""You are the receptionist for {self.name}. This is a live voice call.

Business facts:
{self.info_text()}
Right now it is {now:%A} {now.day} {now:%B} {now:%Y}, {now:%H:%M} ({self.timezone_name}).

Rules:
- Speak in one or two short sentences per turn. Never give a speech.
- Never guess availability. Call check_availability before offering any time.
- Only call book_appointment after the caller agrees to a specific time.
- Before booking, confirm the service, the day and the time back to the caller.
- Say times as words: "Wednesday at three", not "15:00".
- If a requested time is not in the returned slots, offer the nearest ones instead.
- Services you can book: {service_list}.
- If you cannot help, call take_message. Never invent policies or prices.
- Never call end_call in the same turn as a booking or a cancellation. Ask if
  there is anything else first.
- Call end_call only after the caller has said goodbye, or clearly said they are
  finished. If they are still talking, stay on the line.
"""


# -- argument parsing ------------------------------------------------------


def _parse_date(value: object, fallback_tz: ZoneInfo) -> date | None:
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def _parse_datetime(value: object, tz: ZoneInfo) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed


def _error(message: str, **extra) -> dict:
    return {"ok": False, "error": message, **extra}


# -- tool implementations --------------------------------------------------


def check_availability(business: Business, store: Store, args: dict) -> dict:
    first = _parse_date(args.get("date_from"), business.tz)
    if first is None:
        return _error("date_from must be an ISO date like 2026-10-28")

    last = _parse_date(args.get("date_to"), business.tz) if args.get("date_to") else first
    if last is None or last < first:
        last = first + timedelta(days=6)

    service_id = str(args.get("service") or next(iter(business.services)))
    if service_id not in business.services:
        return _error(
            "unknown service",
            valid_services=list(business.services),
        )

    slots = business.available_slots(store, first, last, service_id)
    if not slots:
        return {
            "ok": True,
            "slots": [],
            "note": "No openings in that range. Try the following week.",
        }
    return {
        "ok": True,
        "service": service_id,
        "slots": [
            {"start": slot.isoformat(), "label": business.label(slot)} for slot in slots
        ],
    }


def book_appointment(business: Business, store: Store, args: dict) -> dict:
    service_id = str(args.get("service") or "")
    if service_id not in business.services:
        return _error("unknown service", valid_services=list(business.services))

    name = str(args.get("name") or "").strip()
    phone = str(args.get("phone") or "").strip()
    if not name or not phone:
        return _error("name and phone are required")

    start = _parse_datetime(args.get("start"), business.tz)
    if start is None:
        return _error("start must be an ISO datetime like 2026-10-28T15:00:00+01:00")

    minutes = business.duration_minutes(service_id)
    slot = business.find_slot(start, minutes)
    if slot is None:
        alternatives = business.available_slots(
            store, start.date(), start.date() + timedelta(days=6), service_id
        )
        return _error(
            "that time is not a bookable slot",
            alternatives=[
                {"start": item.isoformat(), "label": business.label(item)}
                for item in alternatives[:4]
            ],
        )

    end = slot + timedelta(minutes=minutes)
    try:
        appointment = store.book(
            slot.astimezone(timezone.utc),
            end.astimezone(timezone.utc),
            service_id,
            name,
            phone,
            str(args.get("notes") or ""),
        )
    except SlotTaken:
        return _error("that time was just taken", alternatives=[])

    return {
        "ok": True,
        "appointment_id": appointment["id"],
        "service": business.service_name(service_id),
        "start": slot.isoformat(),
        "label": business.label(slot),
    }


def find_appointments(business: Business, store: Store, args: dict) -> dict:
    phone = str(args.get("phone") or "").strip()
    if not phone:
        return _error("phone is required")

    rows = store.find_by_phone(phone)
    return {
        "ok": True,
        "appointments": [
            {
                "appointment_id": row["id"],
                "service": business.service_name(row["service"]),
                "start": _parse_datetime(row["start_iso"], business.tz).isoformat(),
                "label": business.label(_parse_datetime(row["start_iso"], business.tz)),
            }
            for row in rows
        ],
    }


def cancel_appointment(business: Business, store: Store, args: dict) -> dict:
    phone = str(args.get("phone") or "").strip()
    try:
        appointment_id = int(args.get("appointment_id"))
    except (TypeError, ValueError):
        return _error("appointment_id must be a number")

    if not phone:
        return _error("phone is required to cancel")

    row = store.cancel(appointment_id, phone)
    if row is None:
        return _error("no appointment with that id matches that phone number")

    moment = _parse_datetime(row["start_iso"], business.tz)
    return {"ok": True, "cancelled": business.label(moment)}


def get_business_info(business: Business, store: Store, args: dict) -> dict:
    return {"ok": True, "info": business.info_text()}


def take_message(business: Business, store: Store, args: dict) -> dict:
    name = str(args.get("name") or "").strip()
    phone = str(args.get("phone") or "").strip()
    reason = str(args.get("reason") or "").strip()
    if not name or not phone or not reason:
        return _error("name, phone and reason are all required")

    message = store.add_message(name, phone, reason)
    return {"ok": True, "message_id": message["id"]}


def end_call(business: Business, store: Store, args: dict) -> dict:
    return {"ok": True, "ending": True}


HANDLERS = {
    "check_availability": check_availability,
    "book_appointment": book_appointment,
    "find_appointments": find_appointments,
    "cancel_appointment": cancel_appointment,
    "get_business_info": get_business_info,
    "take_message": take_message,
    "end_call": end_call,
}


def execute(name: str, args: dict, business: Business, store: Store) -> dict:
    handler = HANDLERS.get(name)
    if handler is None:
        return _error(f"unknown tool {name}")
    try:
        return handler(business, store, args or {})
    except Exception as exc:  # keep the call alive; tell the model what broke
        return _error(f"{type(exc).__name__}: {exc}")


# -- declarations handed to Gemini ----------------------------------------


def _schema(**kwargs) -> types.Schema:
    return types.Schema(**kwargs)


def declarations(business: Business) -> list[types.Tool]:
    service_ids = list(business.services)

    return [
        types.Tool(
            function_declarations=[
                types.FunctionDeclaration(
                    name="check_availability",
                    description=(
                        "List open appointment slots. Always call this before offering a time. "
                        "Returns at most 8 slots, ordered earliest first."
                    ),
                    parameters=_schema(
                        type="OBJECT",
                        properties={
                            "date_from": _schema(
                                type="STRING", description="Start date, ISO 8601, e.g. 2026-10-28"
                            ),
                            "date_to": _schema(
                                type="STRING", description="Optional end date, ISO 8601"
                            ),
                            "service": _schema(type="STRING", enum=service_ids),
                        },
                        required=["date_from"],
                    ),
                ),
                types.FunctionDeclaration(
                    name="book_appointment",
                    description=(
                        "Book one of the slots returned by check_availability. "
                        "Only call after the caller has agreed to a specific time."
                    ),
                    parameters=_schema(
                        type="OBJECT",
                        properties={
                            "service": _schema(type="STRING", enum=service_ids),
                            "start": _schema(
                                type="STRING",
                                description="Exact slot start, ISO 8601, as returned by check_availability",
                            ),
                            "name": _schema(type="STRING", description="Caller's full name"),
                            "phone": _schema(type="STRING", description="Caller's phone number"),
                            "notes": _schema(type="STRING", description="Anything worth noting"),
                        },
                        required=["service", "start", "name", "phone"],
                    ),
                ),
                types.FunctionDeclaration(
                    name="find_appointments",
                    description="List a caller's upcoming appointments by phone number.",
                    parameters=_schema(
                        type="OBJECT",
                        properties={
                            "phone": _schema(type="STRING"),
                        },
                        required=["phone"],
                    ),
                ),
                types.FunctionDeclaration(
                    name="cancel_appointment",
                    description="Cancel an existing appointment. Requires the matching phone number.",
                    parameters=_schema(
                        type="OBJECT",
                        properties={
                            "appointment_id": _schema(type="INTEGER"),
                            "phone": _schema(type="STRING"),
                        },
                        required=["appointment_id", "phone"],
                    ),
                ),
                types.FunctionDeclaration(
                    name="get_business_info",
                    description="Opening hours, address, services. Use instead of guessing.",
                    parameters=_schema(type="OBJECT", properties={}),
                ),
                types.FunctionDeclaration(
                    name="take_message",
                    description="Record a message when the request cannot be handled now.",
                    parameters=_schema(
                        type="OBJECT",
                        properties={
                            "name": _schema(type="STRING"),
                            "phone": _schema(type="STRING"),
                            "reason": _schema(type="STRING"),
                        },
                        required=["name", "phone", "reason"],
                    ),
                ),
                types.FunctionDeclaration(
                    name="end_call",
                    description=(
                        "Hang up. Use only after the caller says goodbye or confirms they are done, "
                        "and never in the same turn as a booking."
                    ),
                    parameters=_schema(type="OBJECT", properties={}),
                ),
            ]
        )
    ]
