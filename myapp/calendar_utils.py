from datetime import date, timedelta

BIWEEKLY_ANCHOR = date(2026, 1, 5)  # A known Monday that starts a biweekly cycle
WEEKDAY_KEYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
WEEKDAY_LABELS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday',
                  'Saturday', 'Sunday']
MEAL_SLOT_LABELS = [
    ('cold_breakfast', 'Cold Bkfst'),
    ('hot_breakfast',  'Hot Bkfst'),
    ('lunch',          'Lunch'),
    ('dinner',         'Dinner'),
]

# Sean 2026-05-03: weekends serve lunch + dinner only (no breakfast).
# Used by calendar grid + signals/preptask auto-gen to suppress the
# breakfast slots on Sat/Sun.
WEEKEND_SERVED_SLOTS = {'lunch', 'dinner'}


def served_slots_for(d: date) -> set[str]:
    """Which meal slots are served on date `d`. Mon-Fri = all 4;
    Sat/Sun = lunch + dinner only."""
    if d.weekday() >= 5:
        return WEEKEND_SERVED_SLOTS
    return {slot for slot, _ in MEAL_SLOT_LABELS}


def biweekly_start_for(d: date) -> date:
    """The Monday that begins the biweekly cycle containing d."""
    offset_days = (d - BIWEEKLY_ANCHOR).days
    return BIWEEKLY_ANCHOR + timedelta(days=(offset_days // 14) * 14)
