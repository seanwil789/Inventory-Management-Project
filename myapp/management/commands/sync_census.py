import csv
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from myapp.models import Census


DATE_FMT = "%A, %B %d, %Y"  # "Sunday, March 1, 2026"


def parse_date(s: str) -> date | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, DATE_FMT).date()
    except ValueError:
        try:
            return datetime.strptime(s, "%m/%d/%Y").date()
        except ValueError:
            return None


def parse_delta(s: str) -> int | None:
    s = (s or "").strip()
    if not s:
        return None
    m = re.match(r"^\((\d+)\)$", s)
    if m:
        return -int(m.group(1))
    try:
        return int(s)
    except ValueError:
        return None


def extract_events(csv_path: Path) -> list[tuple[date, int, str]]:
    """Return (date, delta, note) tuples from the right-hand census block."""
    events: list[tuple[date, int, str]] = []
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.reader(f):
            if len(row) < 8:
                continue
            d = parse_date(row[5])
            if not d:
                continue
            delta = parse_delta(row[7])
            if delta is None:
                continue
            note = (row[6] or "").strip()
            events.append((d, delta, note))
    return events


def build_daily_counts(events: list[tuple[date, int, str]]) -> dict[date, tuple[int, list[str]]]:
    """Walk the event stream forward; fill every day from first→last event with running count + that day's notes."""
    if not events:
        return {}
    events.sort(key=lambda e: e[0])
    by_day: dict[date, list[tuple[int, str]]] = {}
    for d, delta, note in events:
        by_day.setdefault(d, []).append((delta, note))

    start, end = events[0][0], events[-1][0]
    running = 0
    out: dict[date, tuple[int, list[str]]] = {}
    cur = start
    while cur <= end:
        day_events = by_day.get(cur, [])
        for delta, _ in day_events:
            running += delta
        notes = [n for _, n in day_events if n]
        out[cur] = (running, notes)
        cur += timedelta(days=1)
    return out


class Command(BaseCommand):
    help = "Parse the Wentworth budget CSV delta stream and write one Census row per day."

    def add_arguments(self, parser):
        parser.add_argument("csv_path", type=str, help="Path to the budget CSV")
        parser.add_argument("--dry-run", action="store_true", help="Parse and print; do not write")

    def handle(self, *args, **opts):
        path = Path(opts["csv_path"])
        if not path.exists():
            raise CommandError(f"Not found: {path}")

        events = extract_events(path)
        self.stdout.write(f"Parsed {len(events)} census events")
        daily = build_daily_counts(events)
        if not daily:
            self.stdout.write(self.style.WARNING("No daily counts produced — check CSV shape"))
            return

        first = min(daily)
        last = max(daily)
        self.stdout.write(f"Coverage: {first} → {last} ({len(daily)} days)")

        total_person_days = sum(hc for hc, _ in daily.values())
        avg = total_person_days / len(daily)
        self.stdout.write(f"Month-to-date average headcount: {avg:.2f}")

        if opts["dry_run"]:
            sample = sorted(daily.items())
            for d, (hc, notes) in sample[:5] + sample[-5:]:
                note_str = f" — {'; '.join(notes)}" if notes else ""
                self.stdout.write(f"  {d}: {hc}{note_str}")
            return

        created, updated = 0, 0
        for d, (hc, notes) in daily.items():
            note_str = "; ".join(notes)[:200]
            _, was_created = Census.objects.update_or_create(
                date=d, defaults={"headcount": hc, "notes": note_str},
            )
            if was_created:
                created += 1
            else:
                updated += 1
        self.stdout.write(self.style.SUCCESS(f"Wrote Census: {created} created, {updated} updated"))
