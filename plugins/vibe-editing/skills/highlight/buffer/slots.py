"""
Posting slot generator — every hour on the hour +/- jitter.
Prevents spam-looking regular intervals.

(Buffer's own queue schedule normally handles timing via addToQueue. These slots are
here for when you want explicit custom-scheduled times via due_at.)
"""

import random
from datetime import datetime, timedelta, timezone


def generate_slots(
    count: int,
    start: datetime | None = None,
    jitter_minutes: int = 15,
) -> list[datetime]:
    """Generate `count` posting slots at hourly intervals with +/- jitter.

    Each slot is on the hour (XX:00) offset by a random +/- jitter_minutes.
    Slots are always in the future and in UTC.
    """
    now = datetime.now(timezone.utc)
    if start is None:
        start = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    slots = []
    base = start
    for _ in range(count):
        offset = random.randint(-jitter_minutes, jitter_minutes)
        slot = base + timedelta(minutes=offset)
        if slot <= now:
            slot = now + timedelta(minutes=random.randint(5, 20))
        slots.append(slot)
        base += timedelta(hours=1)

    return slots


def format_slot(dt: datetime) -> str:
    """Format a datetime as ISO 8601 UTC for the Buffer API."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def preview_slots(count: int = 10) -> None:
    """Print a preview of the next N posting slots."""
    slots = generate_slots(count)
    print("ESTIMATES ONLY: these are not Buffer's actual queue slots and are not reserved.")
    print(f"Next {count} example slots (UTC):\n")
    for i, s in enumerate(slots, 1):
        local = s.astimezone()
        print(f"  {i:2d}. {format_slot(s)}  ({local.strftime('%I:%M %p %Z')})")


if __name__ == "__main__":
    preview_slots(24)
