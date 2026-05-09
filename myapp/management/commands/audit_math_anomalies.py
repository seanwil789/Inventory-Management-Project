"""Surface ILI rows tagged math_flagged=True for parser-bug diagnosis.

Companion to `audit_parser_garbles`. Same pattern: read-only audit surface
that lets Sean see the anomaly population by vendor + recency for triage.

Math anomalies (per `invoice_processor/line_math.py`) flag rows where
qty × price ≠ extended_amount beyond tolerance (5% AND $2). Catch-weight
aware: uses price_per_pound when populated, unit_price otherwise. So
Exceptional / Sysco MEATS catch-weight rows are NOT flagged just because
qty × unit_price != ext (that's the schema overload, not an anomaly).

Run:
  python manage.py audit_math_anomalies                    # all anomalies
  python manage.py audit_math_anomalies --vendor "Sysco"   # filter
  python manage.py audit_math_anomalies --since 2026-04-01 # date filter
  python manage.py audit_math_anomalies --top 20           # limit output
"""
from collections import Counter
from datetime import date, datetime
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db.models import Count, F

from myapp.models import InvoiceLineItem, Vendor


class Command(BaseCommand):
    help = ('Audit InvoiceLineItem rows flagged with math_flagged=True. '
            'Read-only — surface for human triage.')

    def add_arguments(self, parser):
        parser.add_argument('--vendor', help='Filter to a single vendor name')
        parser.add_argument('--since', help='Filter to invoices on/after this date (YYYY-MM-DD)')
        parser.add_argument('--top', type=int, default=50,
                            help='Max rows to print (default 50)')

    def handle(self, *args, **opts):
        qs = InvoiceLineItem.objects.filter(math_flagged=True)

        if opts.get('vendor'):
            qs = qs.filter(vendor__name=opts['vendor'])

        if opts.get('since'):
            try:
                d = datetime.strptime(opts['since'], '%Y-%m-%d').date()
                qs = qs.filter(invoice_date__gte=d)
            except ValueError:
                self.stdout.write(self.style.ERROR(
                    f"Invalid --since date {opts['since']!r}; expected YYYY-MM-DD."))
                return

        total = qs.count()
        self.stdout.write(f'Math-flagged ILI rows: {total}')

        if total == 0:
            self.stdout.write(self.style.SUCCESS(
                'Clean — no anomalies in scope. (If this is unexpected, '
                'check that backfill_math_flagged has run.)'))
            return

        # By-vendor breakdown
        self.stdout.write('')
        self.stdout.write('By vendor:')
        vendor_rows = (qs.values('vendor__name')
                         .order_by()
                         .annotate(n=Count('id'))
                         .order_by('-n'))
        for r in vendor_rows:
            v = r['vendor__name'] or '(no vendor)'
            self.stdout.write(f'  {v:30} {r["n"]}')

        # By-month breakdown
        self.stdout.write('')
        self.stdout.write('By month:')
        month_counter = Counter()
        for ili in qs.only('invoice_date'):
            if ili.invoice_date:
                key = (ili.invoice_date.year, ili.invoice_date.month)
                month_counter[key] += 1
        for (y, m), n in sorted(month_counter.items()):
            self.stdout.write(f'  {y:04d}-{m:02d}  {n}')

        # Top rows by recency
        self.stdout.write('')
        self.stdout.write(f'Top {opts["top"]} by date (most recent first):')
        self.stdout.write(
            f'  {"date":<11} {"vendor":<22} '
            f'{"qty":>7} {"unit":>9} {"ppp":>9} {"ext":>9}  desc'
        )
        for ili in qs.select_related('vendor').order_by('-invoice_date')[:opts['top']]:
            qty = float(ili.quantity or 0)
            up = float(ili.unit_price or 0)
            ppp = float(ili.price_per_pound or 0)
            ext = float(ili.extended_amount or 0)
            v = (ili.vendor.name if ili.vendor else '-')[:22]
            d = str(ili.invoice_date or '-')[:11]
            self.stdout.write(
                f'  {d:<11} {v:<22} {qty:>7.2f} '
                f'${up:>7.2f} ${ppp:>7.2f} ${ext:>7.2f}  '
                f'{(ili.raw_description or "")[:40]}'
            )
