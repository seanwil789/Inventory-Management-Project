from datetime import date, timedelta

BIWEEKLY_ANCHOR = date(2026, 1, 5)  # A known Monday that starts a biweekly cycle
WEEKDAY_KEYS = ['mon', 'tue', 'wed', 'thu', 'fri']
WEEKDAY_LABELS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']
MEAL_SLOT_LABELS = [
    ('cold_breakfast', 'Cold Bkfst'),
    ('hot_breakfast',  'Hot Bkfst'),
    ('lunch',          'Lunch'),
    ('dinner',         'Dinner'),
]


def biweekly_start_for(d: date) -> date:
    """The Monday that begins the biweekly cycle containing d."""
    offset_days = (d - BIWEEKLY_ANCHOR).days
    return BIWEEKLY_ANCHOR + timedelta(days=(offset_days // 14) * 14)
