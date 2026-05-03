"""Sysco CSV ingestion — updates the SUPC mapping library from a Sysco
portal CSV export without replacing the OCR invoice workflow.

CSV row types:
  H = invoice header   (date, total, etc.)
  F = field definitions
  P = product line     (SUPC | case_qty | split_qty | cust# | pack | brand | description | ...)

For each P row the ingestor:
  1. Skips SUPCs already present in any ProductMapping.
  2. Tries an exact then fuzzy description match against existing
     ProductMapping rows — if found, writes the SUPC into that row.
  3. If no match, enqueues a ProductMappingProposal stub (vendor=Sysco,
     desc=CSV text, supc set, suggested_product=None) so Sean canonicalizes
     it via /mapping-review/'s create-and-approve flow.

Returns a summary dict so batch.py can log the results.

Sean 2026-05-02: refactored from sheet-write (Item Mapping tab) to
ProductMapping + ProductMappingProposal — the sheet's Item Mapping
tab is retired.
"""
import csv
import os
import re

# Bootstrap Django (csv_ingest is called from batch.py which is non-Django entry)
import django
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'myproject.settings')
try:
    django.setup()
except Exception:
    pass  # already set up by caller

from rapidfuzz import process, fuzz

FUZZY_THRESHOLD = 75


def _parse_csv(path: str) -> list[dict]:
    """Return list of product dicts from a Sysco CSV file."""
    items = []
    with open(path, newline='', encoding='utf-8-sig') as f:
        for row in csv.reader(f):
            if not row or row[0] != 'P':
                continue
            while len(row) < 8:
                row.append('')
            supc  = row[1].strip()
            pack  = row[5].strip()
            brand = row[6].strip()
            desc  = row[7].strip()
            if supc and desc:
                items.append({'supc': supc, 'brand': brand, 'desc': desc, 'pack': pack})
    return items


def ingest_csv(path: str, dry_run: bool = False) -> dict:
    """Process one Sysco CSV file and update SUPC bindings.
    Returns summary: {matched, added, skipped, total}.

    matched = SUPC backfilled into an existing ProductMapping row
    added   = ProductMappingProposal stub created for Sean's review
    skipped = SUPC already in code_map / existing PM
    """
    from myapp.models import Vendor, ProductMapping, ProductMappingProposal

    print(f"   Parsing CSV: {os.path.basename(path)}")
    items = _parse_csv(path)
    if not items:
        print("   No product rows found.")
        return {'matched': 0, 'added': 0, 'skipped': 0, 'total': 0}

    print(f"   Found {len(items)} product rows.")

    sysco, _ = Vendor.objects.get_or_create(name='Sysco')

    # Load current ProductMapping state.
    # desc_to_pm: normalized-desc -> PM (for matching CSV desc to existing row)
    # code_set:   SUPCs already curated (skip the CSV entry entirely)
    pms = list(ProductMapping.objects.filter(vendor=sysco)
                                       .select_related('product'))
    desc_to_pm = {}
    code_set = set()
    for pm in pms:
        d = (pm.description or '').strip().upper()
        if d:
            desc_to_pm[d] = pm
        if pm.supc:
            code_set.add(pm.supc.strip())

    # Match each CSV item
    code_updates = []   # (PM, supc) — backfill SUPC on existing row
    stub_creates = []   # CSV item dicts for new ProductMappingProposal stubs
    skipped      = 0

    for item in items:
        supc = item['supc']
        desc = item['desc']
        norm = re.sub(r'\s+', ' ', desc.upper().strip())

        if supc in code_set:
            skipped += 1
            continue

        # Exact match
        if norm in desc_to_pm:
            code_updates.append((desc_to_pm[norm], supc))
            continue

        # Fuzzy match
        result = process.extractOne(norm, desc_to_pm.keys(),
                                    scorer=fuzz.token_sort_ratio)
        if result and result[1] >= FUZZY_THRESHOLD:
            code_updates.append((desc_to_pm[result[0]], supc))
            continue

        # No match — stub for /mapping-review/
        stub_creates.append(item)

    if dry_run:
        print(f"   [DRY RUN] Would update {len(code_updates)} existing PM rows, "
              f"add {len(stub_creates)} stubs, skip {skipped} already-mapped.")
        return {'matched': len(code_updates), 'added': len(stub_creates),
                'skipped': skipped, 'total': len(items)}

    # Apply: update existing PMs with SUPC
    for pm, supc in code_updates:
        pm.supc = supc
        pm.save(update_fields=['supc'])
    if code_updates:
        print(f"   [✓] Backfilled SUPC on {len(code_updates)} existing ProductMapping rows.")

    # Apply: create stub ProductMappingProposals for new SUPCs.
    # source='discover_unmapped' since the semantic is "found this from
    # an external data source, needs canonicalization." suggested_product=
    # None means the create-and-approve flow lets Sean invent a canonical.
    new_proposals = 0
    for item in stub_creates:
        notes = (f"Sysco CSV import · SUPC {item['supc']} · brand={item['brand']} "
                 f"· pack={item['pack']}")
        _, created, _ = ProductMappingProposal.get_or_create_dedup(
            vendor=sysco,
            raw_description=item['desc'],
            suggested_product=None,
            source='discover_unmapped',
            defaults=dict(
                score=None,
                confidence_tier='csv_stub',
                status='pending',
                notes=notes,
            ),
        )
        if created:
            new_proposals += 1
    if new_proposals:
        print(f"   [✓] Enqueued {new_proposals} new SUPC stub(s) in /mapping-review/.")
        print(f"       (canonical name needed; visible at /mapping-review/?status=unresolved)")
    if skipped:
        print(f"   [–] Skipped {skipped} SUPCs already in ProductMapping.")

    return {'matched': len(code_updates), 'added': new_proposals,
            'skipped': skipped, 'total': len(items)}
