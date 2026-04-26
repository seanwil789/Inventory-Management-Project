"""Surface and (optionally) fill missing Product.default_case_size from
each Product's ILI history.

Why this matters: case_size drives IUP (price-per-unit-within-case) and
$/lb math used by inventory valuation. Wrong or missing case_size →
wrong dollar math during the month-end count.

Two-pass derivation per Product with ≥1 ILI but no default_case_size:
  Pass A (ILI structured field):
    Pull distinct InvoiceLineItem.case_size values for the Product.
      - auto:      single distinct, OR clear winner (≥70%, ≥2 samples)
      - ambiguous: multiple distinct values, no clear winner
      - falls through to Pass B if no structured data
  Pass B (regex from raw_description):
    Apply ordered regex patterns to extract case markers embedded in the
    raw text — '10 LB', '1 1/9 BUSHEL', '6/32 OZ', '12 BU', etc.
      - extracted: raw text yielded a structured marker
      - no_data:   nothing structured found (likely per-each items)
  With --apply: fill the auto + extracted buckets. Ambiguous + no_data
  surface for human follow-up.

Usage:
    python manage.py audit_case_size_coverage           # dry-run report
    python manage.py audit_case_size_coverage --apply   # fill auto+extracted
    python manage.py audit_case_size_coverage --verbose # detail view
"""
import re
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count

from myapp.models import Product, InvoiceLineItem


# Regex patterns to extract case_size markers from raw_description.
# Order matters: longest/most-specific patterns first so '1 1/9 BUSHEL'
# matches before '1 BUSHEL'. Capture group → normalized output.
_RAW_PATTERNS = [
    re.compile(r'(\d+[\s\-]\d+/\d+\s*BUSHEL)', re.I),
    re.compile(r'(\d+/\d+\s*BUSHEL)', re.I),
    re.compile(r'(\d+\s*BUSHEL)', re.I),
    re.compile(r'(\d+\s*BU)\b', re.I),
    re.compile(r'(\d+/\d+(?:\.\d+)?\s*(?:OZ|LB|GAL|CT|EA))', re.I),
    re.compile(r'(\d+(?:\.\d+)?\s*LB\s*BAG)\b', re.I),
    re.compile(r'(\d+(?:\.\d+)?\s*LBS?)\b', re.I),
    re.compile(r'(\d+(?:\.\d+)?\s*OZ)\b', re.I),
    re.compile(r'(\d+(?:\.\d+)?\s*GAL)\b', re.I),
    re.compile(r'(\d+\s*CT)\b', re.I),
]


def _extract_from_raw(raw):
    """Try regex patterns in order; return the first capture-group match
    (whitespace-collapsed, upper-cased), or None."""
    if not raw:
        return None
    for pat in _RAW_PATTERNS:
        m = pat.search(raw)
        if m:
            return re.sub(r'\s+', ' ', m.group(1).strip().upper())
    return None


def _derive(product):
    """Return (case_size, kind) where kind in {'auto', 'extracted',
    'ambiguous', 'no_data'}.

    Pass A: try ILI.case_size structured field (auto/ambiguous outcomes).
    Pass B: if no structured data, regex-extract from raw_description.
    """
    distinct = Counter()
    for cs in (InvoiceLineItem.objects.filter(product=product)
               .exclude(case_size='').exclude(case_size__isnull=True)
               .values_list('case_size', flat=True)):
        cs = (cs or '').strip()
        if cs:
            distinct[cs] += 1
    if distinct:
        if len(distinct) == 1:
            return next(iter(distinct.keys())), 'auto'
        top, top_n = distinct.most_common(1)[0]
        total = sum(distinct.values())
        if top_n / total >= 0.7 and top_n >= 2:
            return top, 'auto'
        return None, 'ambiguous'

    # Pass B: regex from raw_description (newest first)
    sample = (InvoiceLineItem.objects.filter(product=product)
              .order_by('-invoice_date').first())
    if sample:
        cs = _extract_from_raw(sample.raw_description)
        if cs:
            return cs, 'extracted'
    return None, 'no_data'


class Command(BaseCommand):
    help = 'Audit and fill missing Product.default_case_size from ILI history.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Fill the auto-fillable bucket. Default is dry-run.')
        parser.add_argument('--verbose', action='store_true',
                            help='List ambiguous + no_data buckets in detail.')

    def handle(self, *args, **opts):
        apply_changes = opts['apply']
        verbose = opts['verbose']
        mode = 'APPLY' if apply_changes else 'DRY-RUN'
        self.stdout.write(f'=== {mode} mode ===\n')

        # Find Products with ≥1 ILI but missing default_case_size
        candidates = (Product.objects
                      .annotate(n_ili=Count('invoicelineitem'))
                      .filter(n_ili__gt=0)
                      .filter(default_case_size__in=['', None])
                      .order_by('canonical_name'))
        total = candidates.count()
        self.stdout.write(f'Products with ILIs but missing default_case_size: {total}\n')

        auto_bucket      = []  # (product, derived_case_size, sample_count, distinct)
        extracted_bucket = []  # (product, extracted_case_size, source_raw)
        ambig_bucket     = []  # (product, distinct_dict)
        no_data_bucket   = []  # (product, ili_count)

        for p in candidates:
            cs, kind = _derive(p)
            if kind == 'auto':
                d = Counter(c.strip() for c in InvoiceLineItem.objects
                            .filter(product=p).exclude(case_size='')
                            .exclude(case_size__isnull=True)
                            .values_list('case_size', flat=True) if c)
                auto_bucket.append((p, cs, sum(d.values()), dict(d)))
            elif kind == 'extracted':
                sample = (InvoiceLineItem.objects.filter(product=p)
                          .order_by('-invoice_date').first())
                src = sample.raw_description[:60] if sample else ''
                extracted_bucket.append((p, cs, src))
            elif kind == 'ambiguous':
                d = Counter(c.strip() for c in InvoiceLineItem.objects
                            .filter(product=p).exclude(case_size='')
                            .exclude(case_size__isnull=True)
                            .values_list('case_size', flat=True) if c)
                ambig_bucket.append((p, dict(d)))
            else:
                no_data_bucket.append((p, p.n_ili))

        self.stdout.write(f'  auto (ILI struct field): {len(auto_bucket)}')
        self.stdout.write(f'  extracted (raw regex):   {len(extracted_bucket)}')
        self.stdout.write(f'  ambiguous:               {len(ambig_bucket)}')
        self.stdout.write(f'  no_data:                 {len(no_data_bucket)}')
        self.stdout.write('')

        # Show auto bucket sample
        self.stdout.write('=== Auto-fillable from ILI structured field (top 20) ===')
        for p, cs, n, _d in auto_bucket[:20]:
            self.stdout.write(f"  {p.canonical_name!r:<35} → case_size={cs!r:<12} ({n} ILI sample)")
        if len(auto_bucket) > 20:
            self.stdout.write(f'  ... +{len(auto_bucket) - 20} more')

        self.stdout.write('')
        self.stdout.write('=== Extracted from raw_description (top 20) ===')
        for p, cs, src in extracted_bucket[:20]:
            self.stdout.write(f"  {p.canonical_name!r:<35} → {cs!r:<14}  (from {src!r})")
        if len(extracted_bucket) > 20:
            self.stdout.write(f'  ... +{len(extracted_bucket) - 20} more')

        self.stdout.write('')
        self.stdout.write('=== Ambiguous (need human decision; top 15 shown) ===')
        for p, d in ambig_bucket[:15]:
            top3 = sorted(d.items(), key=lambda x: -x[1])[:3]
            top_str = ', '.join(f'{cs!r}×{n}' for cs, n in top3)
            self.stdout.write(f"  {p.canonical_name!r:<35} → {top_str}")
        if len(ambig_bucket) > 15:
            self.stdout.write(f'  ... +{len(ambig_bucket) - 15} more')

        if verbose:
            self.stdout.write('')
            self.stdout.write('=== No-data Products (ILIs carry no case_size) ===')
            for p, n in no_data_bucket:
                self.stdout.write(f"  {p.canonical_name!r:<35} ({n} ILIs)")

        # Apply
        if apply_changes:
            self.stdout.write('')
            self.stdout.write('=== APPLYING auto-fill ===')
            n_auto = n_extracted = 0
            with transaction.atomic():
                for p, cs, _n, _d in auto_bucket:
                    p.default_case_size = cs
                    p.save(update_fields=['default_case_size'])
                    n_auto += 1
                for p, cs, _src in extracted_bucket:
                    p.default_case_size = cs
                    p.save(update_fields=['default_case_size'])
                    n_extracted += 1
            self.stdout.write(self.style.SUCCESS(
                f'  Filled {n_auto} from ILI structured + {n_extracted} from raw extraction = {n_auto + n_extracted} total.'))
            self.stdout.write(f'  Remaining: {len(ambig_bucket)} ambiguous + {len(no_data_bucket)} no-data → human follow-up.')
        else:
            self.stdout.write('')
            self.stdout.write(f'(Dry-run — would fill {len(auto_bucket) + len(extracted_bucket)} Products '
                              f'({len(auto_bucket)} auto + {len(extracted_bucket)} extracted). Re-run with --apply.)')
