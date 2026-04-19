#!/usr/bin/env python
"""
Comprehensive variant audit: identifies products that need splitting into variants.
Run: source .venv/bin/activate && DJANGO_SETTINGS_MODULE=myproject.settings python variant_audit.py
"""
import sys
import os
import re
from collections import defaultdict, Counter
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'myproject.settings')

import django
django.setup()

from myapp.models import Product, InvoiceLineItem
from django.db.models import Count, Min, Max, Avg, StdDev, F, Q

OUTPUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'invoice_processor', 'variant_audit_report.txt')


def normalize_desc(desc):
    """Lowercase and strip common noise from descriptions."""
    return re.sub(r'\s+', ' ', desc.lower().strip())


def extract_form_keywords(descriptions):
    """Pull out form/variant keywords from a list of descriptions."""
    form_keywords = [
        'shredded', 'shred', 'sliced', 'slice', 'diced', 'dice',
        'chopped', 'chop', 'whole', 'block', 'loaf', 'ball',
        'fresh', 'frozen', 'canned', 'dried', 'dry', 'dehydrated',
        'raw', 'cooked', 'roasted', 'grilled', 'smoked', 'cured',
        'ground', 'minced', 'patty', 'patties', 'link', 'links', 'bulk',
        'boneless', 'bone-in', 'skin-on', 'skinless',
        'breast', 'thigh', 'wing', 'leg', 'tender', 'tenders',
        'fillet', 'filet', 'steak', 'loin', 'rib', 'chop', 'rack',
        'juice', 'concentrate', 'puree', 'sauce', 'paste', 'powder',
        'oil', 'extract', 'syrup', 'jam', 'jelly', 'preserves',
        'white', 'yellow', 'red', 'green', 'black',
        'organic', 'conventional',
        'penne', 'spaghetti', 'linguine', 'fettuccine', 'rigatoni', 'fusilli',
        'long grain', 'short grain', 'basmati', 'jasmine', 'arborio',
        'russet', 'yukon', 'sweet', 'idaho', 'fingerling', 'red potato',
        'romaine', 'iceberg', 'spring mix', 'arugula', 'spinach', 'kale',
        'cherry', 'grape', 'roma', 'beefsteak', 'plum', 'heirloom', 'sun-dried',
        'cremini', 'portobello', 'shiitake', 'oyster', 'button', 'mixed',
        'atlantic', 'sockeye', 'king', 'coho', 'chinook', 'wild', 'farmed',
        'tilapia', 'cod', 'halibut', 'mahi', 'swordfish', 'tuna', 'shrimp',
        'mozzarella', 'cheddar', 'parmesan', 'provolone', 'swiss', 'american',
        'heavy', 'light', 'half', 'whipping',
        'unsalted', 'salted', 'clarified',
        'strip', 'ribeye', 'sirloin', 'tenderloin', 'chuck', 'brisket', 'flank',
        'baby', 'mini', 'jumbo', 'large', 'medium', 'small', 'petite',
        'peeled', 'unpeeled', 'trimmed', 'untrimmed',
        'blanched', 'marinated', 'seasoned', 'plain', 'breaded',
        'crinkle', 'straight', 'curly', 'waffle', 'wedge', 'tot', 'hash',
        'french', 'steak fry', 'shoestring',
    ]
    found = defaultdict(list)
    for desc in descriptions:
        d = normalize_desc(desc)
        for kw in form_keywords:
            if kw in d:
                found[kw].append(desc)
    return found


def cluster_descriptions(descriptions):
    """Try to cluster descriptions into variant groups based on keyword overlap."""
    clusters = defaultdict(list)
    for desc in descriptions:
        d = normalize_desc(desc)
        # Create a signature from detected form keywords
        sig_parts = []
        # Check for common differentiating patterns
        form_groups = {
            'shredded': ['shred', 'shredded'],
            'sliced': ['sliced', 'slice'],
            'diced': ['diced', 'dice'],
            'block': ['block'],
            'loaf': ['loaf'],
            'ball': ['ball'],
            'whole': ['whole'],
            'ground': ['ground', 'minced'],
            'fresh': ['fresh'],
            'frozen': ['frozen', 'frz', 'frzn'],
            'canned': ['canned', 'can '],
            'dried': ['dried', 'dry ', 'dehydrated'],
            'raw': ['raw'],
            'cooked': ['cooked', 'ckd', 'roasted', 'grilled'],
            'smoked': ['smoked', 'smk'],
            'boneless': ['boneless', 'bnls'],
            'bone-in': ['bone-in', 'bone in'],
            'breast': ['breast', 'brst'],
            'thigh': ['thigh'],
            'wing': ['wing'],
            'tender': ['tender'],
            'patty': ['patty', 'patties'],
            'link': ['link', 'links'],
            'bulk': ['bulk'],
            'fillet': ['fillet', 'filet'],
            'steak': ['steak'],
            'loin': ['loin'],
            'juice': ['juice', 'jce'],
            'sauce': ['sauce'],
            'paste': ['paste'],
            'powder': ['powder', 'pwdr'],
            'oil': [' oil '],
            'concentrate': ['concentrate', 'conc'],
            'puree': ['puree', 'purée'],
        }
        for group_name, patterns in form_groups.items():
            for pat in patterns:
                if pat in d:
                    sig_parts.append(group_name)
                    break
        sig = '|'.join(sorted(set(sig_parts))) if sig_parts else 'unclassified'
        clusters[sig].append(desc)
    return dict(clusters)


def analyze_product(product):
    """Analyze a single product for variant splitting needs."""
    line_items = InvoiceLineItem.objects.filter(product=product)
    count = line_items.count()
    if count == 0:
        return None

    # Gather all data
    items_data = list(line_items.values(
        'raw_description', 'unit_price', 'case_size', 'vendor__name',
        'invoice_date', 'source_file'
    ))

    # Unique descriptions
    descriptions = list(set(item['raw_description'] for item in items_data if item['raw_description']))
    if not descriptions:
        return None

    # Price analysis
    prices = [float(item['unit_price']) for item in items_data if item['unit_price'] is not None and float(item['unit_price']) > 0]
    if not prices:
        price_min = price_max = price_avg = price_ratio = 0
    else:
        price_min = min(prices)
        price_max = max(prices)
        price_avg = sum(prices) / len(prices)
        price_ratio = price_max / price_min if price_min > 0 else 0

    # Case size analysis
    case_sizes = list(set(item['case_size'] for item in items_data if item['case_size']))

    # Vendor analysis
    vendors = list(set(item['vendor__name'] for item in items_data if item['vendor__name']))

    # Cluster descriptions
    clusters = cluster_descriptions(descriptions)

    # Form keyword extraction
    form_kws = extract_form_keywords(descriptions)

    # Score how likely this product needs splitting
    needs_split = False
    reasons = []
    split_score = 0

    # Multiple distinct clusters
    real_clusters = {k: v for k, v in clusters.items() if k != 'unclassified'}
    if len(real_clusters) > 1:
        needs_split = True
        reasons.append(f"{len(real_clusters)} distinct form clusters detected")
        split_score += len(real_clusters) * 15

    # High price ratio
    if price_ratio >= 3.0 and len(prices) >= 3:
        needs_split = True
        reasons.append(f"Price ratio {price_ratio:.1f}x (${price_min:.2f} - ${price_max:.2f})")
        split_score += min(int(price_ratio * 5), 40)

    # Many different case sizes (more than 3 unique)
    if len(case_sizes) > 3:
        reasons.append(f"{len(case_sizes)} different case sizes")
        split_score += len(case_sizes) * 3

    # Many different descriptions (more than 4 unique)
    if len(descriptions) > 4:
        reasons.append(f"{len(descriptions)} distinct raw descriptions")
        split_score += min(len(descriptions) * 2, 20)

    # Check if unclassified cluster contains obviously different items
    # by looking at the diversity of first-two-word patterns
    if 'unclassified' in clusters and len(clusters['unclassified']) > 2:
        first_words = set()
        for d in clusters['unclassified']:
            words = normalize_desc(d).split()[:3]
            first_words.add(' '.join(words))
        if len(first_words) > 3:
            reasons.append(f"High description diversity even without form keywords ({len(first_words)} patterns)")
            split_score += 10

    # Build per-cluster price analysis
    cluster_details = {}
    for cluster_name, cluster_descs in clusters.items():
        cluster_descs_norm = set(normalize_desc(d) for d in cluster_descs)
        cluster_items = [
            item for item in items_data
            if item['raw_description'] and normalize_desc(item['raw_description']) in cluster_descs_norm
        ]
        c_prices = [float(item['unit_price']) for item in cluster_items if item['unit_price'] is not None and float(item['unit_price']) > 0]
        c_case_sizes = list(set(item['case_size'] for item in cluster_items if item['case_size']))
        c_vendors = list(set(item['vendor__name'] for item in cluster_items if item['vendor__name']))
        cluster_details[cluster_name] = {
            'descriptions': cluster_descs,
            'item_count': len(cluster_items),
            'price_range': (min(c_prices), max(c_prices)) if c_prices else (0, 0),
            'price_avg': sum(c_prices) / len(c_prices) if c_prices else 0,
            'case_sizes': c_case_sizes,
            'vendors': c_vendors,
        }

    return {
        'product': product,
        'canonical_name': product.canonical_name,
        'category': product.category,
        'total_items': count,
        'descriptions': descriptions,
        'prices': prices,
        'price_min': price_min,
        'price_max': price_max,
        'price_avg': price_avg,
        'price_ratio': price_ratio,
        'case_sizes': case_sizes,
        'vendors': vendors,
        'clusters': clusters,
        'cluster_details': cluster_details,
        'form_keywords': dict(form_kws),
        'needs_split': needs_split or split_score >= 25,
        'split_score': split_score,
        'reasons': reasons,
    }


def suggest_variant_names(result):
    """Suggest variant names based on cluster analysis."""
    suggestions = []
    base_name = result['canonical_name']
    cluster_details = result['cluster_details']

    for cluster_name, details in cluster_details.items():
        if cluster_name == 'unclassified' and len(cluster_details) > 1:
            # Try to name the unclassified cluster
            # Look at descriptions for common words
            desc_words = Counter()
            for d in details['descriptions']:
                for w in normalize_desc(d).split():
                    if len(w) > 2 and w.lower() not in base_name.lower().split():
                        desc_words[w] += 1
            top_word = desc_words.most_common(1)
            if top_word:
                variant_name = f"{base_name} ({top_word[0][0].title()})"
            else:
                variant_name = f"{base_name} (Other)"
        elif cluster_name == 'unclassified':
            variant_name = base_name  # single cluster, keep as is
        else:
            parts = cluster_name.split('|')
            descriptor = ', '.join(p.title() for p in parts)
            variant_name = f"{base_name} ({descriptor})"

        # Derive routing keywords
        routing_keywords = []
        for d in details['descriptions']:
            d_norm = normalize_desc(d)
            for part in cluster_name.split('|'):
                if part in d_norm and part not in routing_keywords:
                    routing_keywords.append(part)

        suggestions.append({
            'variant_name': variant_name,
            'cluster_key': cluster_name,
            'item_count': details['item_count'],
            'descriptions': details['descriptions'],
            'price_range': details['price_range'],
            'price_avg': details['price_avg'],
            'case_sizes': details['case_sizes'],
            'vendors': details['vendors'],
            'routing_keywords': routing_keywords,
        })

    return suggestions


def generate_report():
    """Main report generation."""
    products = Product.objects.annotate(
        li_count=Count('invoicelineitem')
    ).filter(li_count__gt=0).order_by('-li_count')

    all_results = []
    needs_splitting = []
    clean_products = []

    for product in products:
        result = analyze_product(product)
        if result is None:
            continue
        all_results.append(result)
        if result['needs_split']:
            result['suggestions'] = suggest_variant_names(result)
            needs_splitting.append(result)
        else:
            clean_products.append(result)

    # Sort by split score (priority)
    needs_splitting.sort(key=lambda x: x['split_score'], reverse=True)

    # Build report
    lines = []
    lines.append("=" * 80)
    lines.append("PRODUCT VARIANT AUDIT REPORT")
    lines.append(f"Generated: 2026-04-13")
    lines.append("=" * 80)
    lines.append("")

    # --- Summary ---
    lines.append("-" * 80)
    lines.append("SECTION 1: SUMMARY")
    lines.append("-" * 80)
    lines.append(f"Total products with line items: {len(all_results)}")
    lines.append(f"Products needing splitting:     {len(needs_splitting)}")
    lines.append(f"Clean products (no split):      {len(clean_products)}")
    lines.append(f"Total line items in DB:         {InvoiceLineItem.objects.count()}")
    lines.append("")

    affected_items = sum(r['total_items'] for r in needs_splitting)
    lines.append(f"Line items affected by splits:  {affected_items}")
    lines.append(f"Line items in clean products:   {sum(r['total_items'] for r in clean_products)}")
    lines.append("")

    # --- Priority ranking ---
    lines.append("-" * 80)
    lines.append("SECTION 2: PRIORITY RANKING (products needing splitting)")
    lines.append("-" * 80)
    lines.append("")
    lines.append(f"{'Rank':<5} {'Score':<7} {'Items':<7} {'Product':<35} {'Reasons'}")
    lines.append(f"{'----':<5} {'-----':<7} {'-----':<7} {'-------':<35} {'-------'}")
    for i, r in enumerate(needs_splitting, 1):
        reason_str = '; '.join(r['reasons'][:2])
        lines.append(f"{i:<5} {r['split_score']:<7} {r['total_items']:<7} {r['canonical_name']:<35} {reason_str}")
    lines.append("")

    # --- Detailed analysis for each product needing split ---
    lines.append("-" * 80)
    lines.append("SECTION 3: DETAILED ANALYSIS — PRODUCTS NEEDING SPLITTING")
    lines.append("-" * 80)

    for i, r in enumerate(needs_splitting, 1):
        lines.append("")
        lines.append(f"{'='*70}")
        lines.append(f"#{i} — {r['canonical_name']}  (Category: {r['category']})")
        lines.append(f"   Split Score: {r['split_score']}  |  Total Line Items: {r['total_items']}")
        lines.append(f"   Price Range: ${r['price_min']:.2f} – ${r['price_max']:.2f}  (ratio: {r['price_ratio']:.1f}x)")
        lines.append(f"   Case Sizes: {', '.join(r['case_sizes'][:10]) if r['case_sizes'] else 'N/A'}")
        lines.append(f"   Vendors: {', '.join(r['vendors'][:5])}")
        lines.append(f"   Reasons: {'; '.join(r['reasons'])}")
        lines.append("")

        lines.append(f"   ALL RAW DESCRIPTIONS ({len(r['descriptions'])}):")
        for d in sorted(r['descriptions']):
            lines.append(f"     - {d}")
        lines.append("")

        lines.append(f"   PROPOSED VARIANTS:")
        for j, sug in enumerate(r.get('suggestions', []), 1):
            lines.append(f"     Variant {j}: {sug['variant_name']}")
            lines.append(f"       Line items: {sug['item_count']}")
            pmin, pmax = sug['price_range']
            lines.append(f"       Price range: ${pmin:.2f} – ${pmax:.2f}  (avg: ${sug['price_avg']:.2f})")
            lines.append(f"       Case sizes: {', '.join(sug['case_sizes'][:5]) if sug['case_sizes'] else 'N/A'}")
            lines.append(f"       Vendors: {', '.join(sug['vendors'][:5]) if sug['vendors'] else 'N/A'}")
            if sug['routing_keywords']:
                lines.append(f"       Routing keywords: {', '.join(sug['routing_keywords'])}")
            lines.append(f"       Sample descriptions:")
            for d in sug['descriptions'][:5]:
                lines.append(f"         - {d}")
            if len(sug['descriptions']) > 5:
                lines.append(f"         ... and {len(sug['descriptions'])-5} more")
            lines.append("")

    # --- Clean products ---
    lines.append("-" * 80)
    lines.append("SECTION 4: CLEAN PRODUCTS (no splitting needed)")
    lines.append("-" * 80)
    lines.append("")
    lines.append(f"{'Product':<40} {'Items':<7} {'Descs':<7} {'Price Range':<25} {'Case Sizes'}")
    lines.append(f"{'-------':<40} {'-----':<7} {'-----':<7} {'-----------':<25} {'----------'}")
    for r in sorted(clean_products, key=lambda x: x['total_items'], reverse=True):
        pr = f"${r['price_min']:.2f}–${r['price_max']:.2f}" if r['prices'] else "N/A"
        cs = ', '.join(r['case_sizes'][:3]) if r['case_sizes'] else 'N/A'
        lines.append(f"{r['canonical_name']:<40} {r['total_items']:<7} {len(r['descriptions']):<7} {pr:<25} {cs}")
    lines.append("")

    # --- Borderline products ---
    lines.append("-" * 80)
    lines.append("SECTION 5: BORDERLINE PRODUCTS (worth a second look)")
    lines.append("-" * 80)
    lines.append("")
    borderline = [r for r in clean_products if r['split_score'] >= 10]
    if borderline:
        borderline.sort(key=lambda x: x['split_score'], reverse=True)
        for r in borderline:
            lines.append(f"  {r['canonical_name']} (score: {r['split_score']}, items: {r['total_items']})")
            lines.append(f"    Reasons: {'; '.join(r['reasons']) if r['reasons'] else 'Low-level signals'}")
            lines.append(f"    Descriptions: {', '.join(r['descriptions'][:5])}")
            lines.append(f"    Price: ${r['price_min']:.2f}–${r['price_max']:.2f} (ratio: {r['price_ratio']:.1f}x)")
            lines.append("")
    else:
        lines.append("  None found.")
    lines.append("")

    lines.append("=" * 80)
    lines.append("END OF REPORT")
    lines.append("=" * 80)

    report_text = '\n'.join(lines)

    # Write to file
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, 'w') as f:
        f.write(report_text)

    print(f"Report written to {OUTPUT_PATH}")
    print(f"\nQuick Summary:")
    print(f"  Products analyzed: {len(all_results)}")
    print(f"  Need splitting: {len(needs_splitting)}")
    print(f"  Clean: {len(clean_products)}")
    print(f"  Top candidates:")
    for r in needs_splitting[:10]:
        print(f"    - {r['canonical_name']} (score={r['split_score']}, {r['total_items']} items, {r['price_ratio']:.1f}x price ratio)")


if __name__ == '__main__':
    generate_report()
