"""Audit ILI rows whose source_file is a Drive filename (e.g. '20260101_151903.jpg')
rather than a SHA cache hash — these may be orphans left behind when the
reprocess pipeline switched from filename-keyed to hash-keyed source_file.

For each filename-source ILI row, classify:
  HIGH_CONF_ORPHAN  — sibling rows under SHA source for same (vendor, date)
                      with matching canonical_vendor_pricelist (covers item) AND
                      matching extended_amount (same line, same money). Safe
                      to delete; SHA copy already represents this line.

  LOW_CONF_ORPHAN   — sibling rows exist for same (vendor, date) but FK or
                      ext differs. May be a duplicate, may be a legitimately
                      different item. Surface for human review.

  UNIQUE_DATA       — no SHA-source sibling rows in same (vendor, date). The
                      filename row is the only record of this line. Do NOT
                      delete; parser may have skipped this item under the
                      SHA-keyed path.

Read-only. To delete HIGH_CONF orphans, this command would need an --apply
flag; intentionally omitted in this version. Run, review output, then
decide cleanup strategy with Sean before adding deletion.
"""
from __future__ import annotations

import re
from collections import defaultdict

from django.core.management.base import BaseCommand

from myapp.models import InvoiceLineItem


_FILENAME_RE = re.compile(r'\.(jpg|jpeg|png|pdf)$', re.I)
_SHA_RE = re.compile(r'^[a-f0-9]{16}(\+\d+)?$')


def _is_filename(sf: str) -> bool:
    return bool(sf and _FILENAME_RE.search(sf))


def _is_sha(sf: str) -> bool:
    return bool(sf and _SHA_RE.match(sf))


class Command(BaseCommand):
    help = ('Audit ILI rows with Drive-filename source_file values for '
            'orphan classification. Read-only.')

    def add_arguments(self, parser):
        parser.add_argument('--vendor', default='',
                            help='Limit to one vendor name (substring match).')
        parser.add_argument('--show-all', action='store_true',
                            help='Show every row, not just summary.')

    def handle(self, *args, **opts):
        qs = InvoiceLineItem.objects.select_related('vendor', 'product')
        if opts['vendor']:
            qs = qs.filter(vendor__name__icontains=opts['vendor'])

        filename_rows = [r for r in qs if _is_filename(r.source_file or '')]
        if not filename_rows:
            self.stdout.write('No filename-source ILI rows found.')
            return

        # Bucket SHA-source siblings by (vendor_id, invoice_date)
        sha_by_bucket: dict = defaultdict(list)
        sha_qs = qs.filter(source_file__regex=r'^[a-f0-9]{16}')
        for r in sha_qs:
            if not _is_sha(r.source_file or ''):
                continue
            sha_by_bucket[(r.vendor_id, r.invoice_date)].append(r)

        classifications = {'HIGH_CONF_ORPHAN': [], 'LOW_CONF_ORPHAN': [],
                           'UNIQUE_DATA': []}

        for r in filename_rows:
            siblings = sha_by_bucket.get((r.vendor_id, r.invoice_date), [])
            if not siblings:
                classifications['UNIQUE_DATA'].append((r, None))
                continue

            # HIGH_CONF: sibling in same (vendor, date) bucket where one of:
            #   (a) FK + ext both match (same product, same line, same money), or
            #   (b) ext matches AND raw_description tokens overlap (parser-variant
            #       of same line — both rows have null FK because mapping
            #       hasn't run yet, but the ext match plus a shared word
            #       fragment confirms identity).
            high_match = None
            for s in siblings:
                ext_match = (r.extended_amount is not None
                             and s.extended_amount == r.extended_amount)
                fk_match = (r.canonical_vendor_pricelist_id is not None
                            and s.canonical_vendor_pricelist_id ==
                            r.canonical_vendor_pricelist_id)
                if fk_match and ext_match:
                    high_match = s
                    break
                if ext_match:
                    # raw token overlap: at least one alphabetic 3+ char token
                    # in common (case-insensitive)
                    r_tokens = set(re.findall(r'[A-Za-z]{3,}',
                                              (r.raw_description or '').upper()))
                    s_tokens = set(re.findall(r'[A-Za-z]{3,}',
                                              (s.raw_description or '').upper()))
                    if r_tokens & s_tokens:
                        high_match = s
                        break
            if high_match:
                classifications['HIGH_CONF_ORPHAN'].append((r, high_match))
                continue

            # LOW_CONF: sibling with same FK OR same ext (but not both)
            low_match = None
            for s in siblings:
                fk_match = (r.canonical_vendor_pricelist_id is not None
                            and s.canonical_vendor_pricelist_id ==
                            r.canonical_vendor_pricelist_id)
                ext_match = (r.extended_amount is not None
                             and s.extended_amount == r.extended_amount)
                if fk_match or ext_match:
                    low_match = s
                    break
            if low_match:
                classifications['LOW_CONF_ORPHAN'].append((r, low_match))
            else:
                classifications['UNIQUE_DATA'].append((r, None))

        # Summary
        self.stdout.write('')
        self.stdout.write('=== ORPHAN CLASSIFICATION ===')
        self.stdout.write(f'Filename-source ILI rows: {len(filename_rows)}')
        self.stdout.write(f'  HIGH_CONF_ORPHAN  (FK+ext match SHA sibling): '
                          f'{len(classifications["HIGH_CONF_ORPHAN"])}')
        self.stdout.write(f'  LOW_CONF_ORPHAN   (FK or ext match):         '
                          f'{len(classifications["LOW_CONF_ORPHAN"])}')
        self.stdout.write(f'  UNIQUE_DATA       (no SHA sibling):          '
                          f'{len(classifications["UNIQUE_DATA"])}')

        # By vendor
        self.stdout.write('')
        self.stdout.write('By vendor:')
        by_vendor: dict = defaultdict(lambda: defaultdict(int))
        for cat, items in classifications.items():
            for r, _ in items:
                by_vendor[r.vendor.name][cat] += 1
        for v, cats in sorted(by_vendor.items()):
            total = sum(cats.values())
            self.stdout.write(
                f'  {v:<25}  {total:>3} total  '
                f'high={cats["HIGH_CONF_ORPHAN"]:>2}  '
                f'low={cats["LOW_CONF_ORPHAN"]:>2}  '
                f'unique={cats["UNIQUE_DATA"]:>2}'
            )

        if not opts['show_all']:
            self.stdout.write('')
            self.stdout.write('Re-run with --show-all to see every row.')
            return

        # Detailed listing
        for cat, items in classifications.items():
            if not items:
                continue
            self.stdout.write('')
            self.stdout.write(f'--- {cat} ({len(items)}) ---')
            for r, sibling in items[:50]:
                ext = r.extended_amount
                fk = r.canonical_vendor_pricelist_id
                self.stdout.write(
                    f'  ili#{r.id:>5} {r.vendor.name[:10]:<10} {r.invoice_date} '
                    f'src={r.source_file[:30]:<30} '
                    f'ext=${ext}  fk={fk}  raw="{(r.raw_description or "")[:40]}"'
                )
                if sibling:
                    self.stdout.write(
                        f'    └─ SHA sibling ili#{sibling.id} '
                        f'src={sibling.source_file} ext=${sibling.extended_amount} '
                        f'fk={sibling.canonical_vendor_pricelist_id}'
                    )
            if len(items) > 50:
                self.stdout.write(f'  ... +{len(items)-50} more')
