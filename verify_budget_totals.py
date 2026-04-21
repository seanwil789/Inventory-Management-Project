"""Investigate Sysco inv #1282480 (no delivery date) + item-extraction gaps.

Not a verification — a diagnostic dump. Prints raw OCR sections for
problem invoices alongside what InvoiceLineItem captured.
"""
import os, sys, json, glob, re

os.chdir('/home/seanwil789/my-saas')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'myproject.settings')
sys.path.insert(0, os.path.abspath('.'))
sys.path.insert(0, os.path.abspath('invoice_processor'))

import django
django.setup()

from myapp.models import InvoiceLineItem

# ── Find Sysco OCR caches by invoice_number in raw text ──────────────────────
def find_sysco_by_inv(inv_num):
    hits = []
    for p in glob.glob('.ocr_cache/*_docai_ocr.json'):
        try:
            with open(p) as f:
                d = json.load(f)
            if d.get('vendor') != 'Sysco':
                continue
            text = d.get('raw_text', '')
            if inv_num in text:
                hits.append((os.path.basename(p), d.get('invoice_date'), text))
        except Exception:
            pass
    return hits

def show_section(label, text, *patterns, context=2):
    print(f"\n--- {label} ---")
    lines = text.split('\n')
    shown = set()
    for i, line in enumerate(lines):
        for pat in patterns:
            if re.search(pat, line, re.IGNORECASE):
                lo = max(0, i - context)
                hi = min(len(lines), i + context + 1)
                for j in range(lo, hi):
                    if j not in shown:
                        shown.add(j)
                        mark = '>' if j == i else ' '
                        print(f"  {mark} L{j:3d}: {lines[j][:120]}")

# ── 1. Inv #1282480: no delivery date found ──────────────────────────────────
print("═" * 70)
print("1. SYSCO INV #1282480 — no delivery date parsed")
print("═" * 70)
hits = find_sysco_by_inv('1282480')
print(f"Cache hits: {len(hits)}")
for fname, dt, text in hits:
    print(f"\nFile: {fname}  (cache-recorded invoice_date: {dt})")
    print(f"Raw text length: {len(text)}")
    show_section("Date fields", text, r'delv', r'deliv', r'inv\.?\s*date', r'\bdate\b', context=1)
    show_section("Invoice number context", text, r'1282480', r'invoice\s*no', r'invoice\s*#', context=2)
    show_section("Total fields", text, r'^\s*TOTAL', r'invoice\s*total', r'2559', r'2506', context=3)

# ── 2. Inv #775793805: 80% items gap (Apr 6 Sysco $788.78) ───────────────────
print("\n" + "═" * 70)
print("2. SYSCO INV #775793805 — 80% item-extraction gap (Apr 6, $788.78)")
print("═" * 70)
hits = find_sysco_by_inv('775793805')
print(f"Cache hits: {len(hits)}")
for fname, dt, text in hits:
    print(f"\nFile: {fname}  (cache-recorded invoice_date: {dt})")
    # Extract item-looking lines
    item_lines = [l for l in text.split('\n') if re.search(r'^\s*\d{6,}\s', l) or re.search(r'\d+\.\d{2}\s*$', l)]
    print(f"Item-shaped lines in OCR: {len(item_lines)}")
    print("First 20 item-shaped lines:")
    for l in item_lines[:20]:
        print(f"  {l[:120]}")

# Items in DB for this invoice
print("\nDB rows for Apr 6 Sysco:")
db_items = InvoiceLineItem.objects.filter(
    vendor__name='Sysco', invoice_date='2026-04-06'
).values('raw_description', 'unit_price', 'case_size', 'source_file')[:50]
for i in db_items:
    print(f"  ${i['unit_price']!s:>8} case={i['case_size']!s:<6}  {i['raw_description'][:70]}  [{i['source_file'][:20]}]")
print(f"Total Apr 6 Sysco DB rows: {InvoiceLineItem.objects.filter(vendor__name='Sysco', invoice_date='2026-04-06').count()}")

# ── 3. Inv #775808085: 31% gap (Apr 13 Sysco $883.31) ────────────────────────
print("\n" + "═" * 70)
print("3. SYSCO INV #775808085 — 31% item-extraction gap (Apr 13, $883.31)")
print("═" * 70)
hits = find_sysco_by_inv('775808085')
print(f"Cache hits: {len(hits)}")
for fname, dt, text in hits:
    print(f"\nFile: {fname}  (cache-recorded invoice_date: {dt})")
    item_lines = [l for l in text.split('\n') if re.search(r'^\s*\d{6,}\s', l) or re.search(r'\d+\.\d{2}\s*$', l)]
    print(f"Item-shaped lines in OCR: {len(item_lines)}")

db_count = InvoiceLineItem.objects.filter(vendor__name='Sysco', invoice_date='2026-04-13').count()
print(f"\nTotal Apr 13 Sysco DB rows: {db_count}")
