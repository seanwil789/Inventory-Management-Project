"""Monthly Synergy tab creation + carryover population.

Run by system cron on day 1 of each month to guarantee the tab exists
with fresh carryover prices regardless of whether anyone has run
batch.py yet. Safe to run any time — idempotent: if the tab already
exists it's a no-op (won't re-clone or double-populate).

Crontab entry (runs at 00:05 on day 1 of every month):
    5 0 1 * * cd /home/seanwil789/my-saas && \\
        .venv/bin/python manage.py create_monthly_synergy_tab \\
        >> /home/seanwil789/my-saas/logs/monthly_tab.log 2>&1

Usage:
    python manage.py create_monthly_synergy_tab          # run for current month
    python manage.py create_monthly_synergy_tab --year 2026 --month 5
    python manage.py create_monthly_synergy_tab --dry-run
"""
from datetime import date
from calendar import month_name

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create the current month's Synergy tab and populate with carryover prices. Idempotent."

    def add_arguments(self, parser):
        parser.add_argument('--year', type=int, default=None,
                            help='Year (default: current)')
        parser.add_argument('--month', type=int, default=None,
                            help='Month 1-12 (default: current)')
        parser.add_argument('--dry-run', action='store_true',
                            help='Preview only — no sheet changes')

    def handle(self, *args, **opts):
        import sys, os
        from django.conf import settings
        invoice_path = str(settings.BASE_DIR / 'invoice_processor')
        if invoice_path not in sys.path:
            sys.path.insert(0, invoice_path)
        from synergy_sync import (
            _list_synergy_tabs, create_month_sheet, refresh_stale_carryover,
        )
        from sheets import get_sheets_client

        today = date.today()
        year = opts['year'] or today.year
        month = opts['month'] or today.month
        dry = opts['dry_run']

        expected_tab = f"Synergy {month_name[month][:3]} {year}"
        self.stdout.write(f"Target tab: {expected_tab}")

        client = get_sheets_client()
        existing = _list_synergy_tabs(client)

        if expected_tab in existing:
            self.stdout.write(self.style.WARNING(
                f"Tab already exists — checking carryover completeness "
                f"(safe to re-run; blank rows will refresh, populated rows stay)."
            ))
        else:
            if dry:
                self.stdout.write(f"[DRY RUN] Would create '{expected_tab}'")
            else:
                new_tab = create_month_sheet(year, month)
                self.stdout.write(self.style.SUCCESS(
                    f"[✓] Created tab: {new_tab}"
                ))

        # Carryover — safe whether tab was just created or already existed.
        # For an existing tab, refresh_stale_carryover only touches rows
        # without a current-month invoice, so it won't clobber fresh prices.
        if dry:
            summary = refresh_stale_carryover(
                sheet_tab=expected_tab, dry_run=True)
            self.stdout.write(
                f"[DRY RUN] Would refresh: {summary['refreshed']}  |  "
                f"Skipped current-month: {summary['skipped_current_month']}  |  "
                f"No history: {summary['skipped_no_history']}"
            )
        else:
            summary = refresh_stale_carryover(sheet_tab=expected_tab)
            self.stdout.write(self.style.SUCCESS(
                f"[✓] Carryover: refreshed {summary['refreshed']}  |  "
                f"already-fresh {summary['skipped_current_month']}  |  "
                f"no history {summary['skipped_no_history']}"
            ))
