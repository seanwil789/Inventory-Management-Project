"""
Case size decoder — extracts pack sizes from invoice descriptions.

Used by the case size cleanup process to automatically populate case_size
from Sysco, Farm Art, and Exceptional Foods raw descriptions.
"""
import re

COMMON_PACK_COUNTS = [1,2,3,4,5,6,8,10,12,15,16,18,20,24,30,32,36,40,48,50,60,
                      72,80,96,100,120,144,150,160,180,192,200,240,250,288,300,
                      500,1000,2000]

COMMON_PER_UNIT = {
    'OZ': [0.5,1,1.3,1.5,2,2.38,2.6,2.75,3,3.25,3.8,4,5,5.3,6,7,8,10,12,
           13.7,14,16,17,18,20,21,22.75,24,28,29,32,34,35,38,42,45,46,48,
           50,56,59,64,85,105,126,128,136],
    'LB': [0.5,1,2,2.2,2.5,3,3.5,4,5,5.75,6,6.6,7.5,7.51,8.6,9.8,10,
           10.55,11,12.5,13.1,15,17.3,17.9,19.3,20,22,22.92,23,24,25,30,
           35,40,42.5,50],
    'GAL': [0.5,1,2.5,5,12.5],
    'CT': None,  # any count valid
    'LTR': [1,2,2.5,5],
    'QT': [1],
    'L': [0.5,1,2],
}

COUNT_PREFERENCE = {
    'LB':'low', 'GAL':'low', 'LTR':'low',
    'OZ':'high', 'CT':'high', 'L':'high', 'QT':'high',
}


def _preprocess_sysco(desc):
    """Fix common OCR run-ons: insert space between unit and next word."""
    # "122.380ZPRINGLE" → "122.38 OZ PRINGLE" (0Z = OZ)
    desc = re.sub(r'(\d)0Z([A-Z])', r'\1 OZ \2', desc)
    # "3.25LBIMP" → "3.25 LB IMP"
    desc = re.sub(r'(\d)(LB|OZ|GAL|CT|EA)([A-Z])', r'\1 \2 \3', desc, flags=re.IGNORECASE)
    # "12.5GALSEAGRAM" → "12.5 GAL SEAGRAM"  (already covered above)
    return desc


def decode_sysco(desc):
    """Extract pack size from a Sysco raw description."""
    desc = _preprocess_sysco(desc)
    for unit in ['OZ','LB','GAL','CT','EA','LTR','QT','0Z','L']:
        real_unit = 'OZ' if unit == '0Z' else unit

        # Use lookahead instead of \b — only prevent eating into next number
        unit_end = rf'(?=[^0-9]|$)'

        # Pattern 1: "ONLY N.N UNIT" or "ONLY NUNIT"
        m = re.match(rf'^ONLY\s*(\d+\.?\d*)\s*{unit}{unit_end}', desc, re.IGNORECASE)
        if m:
            return f'1/{m.group(1)}{real_unit}'

        # Pattern 2: digits (with optional decimal) + unit at start
        m = re.match(rf'^(\d+\.?\d*)\s*{unit}{unit_end}', desc, re.IGNORECASE)
        if not m:
            continue

        raw_full = m.group(1)  # e.g., "482.6" or "2416" or "12.5"

        # Split into integer part and decimal part
        if '.' in raw_full:
            int_part, dec_part = raw_full.split('.', 1)
        else:
            int_part, dec_part = raw_full, None

        candidates = []

        # Strategy A: split the INTEGER part as count+per, decimal goes with per
        # e.g., "482.6" → int_part="482" → try count=48, per="2.6"
        for count in COMMON_PACK_COUNTS:
            cs = str(count)
            if int_part.startswith(cs) and len(int_part) > len(cs):
                per_str = int_part[len(cs):]
                if per_str and per_str[0] == '0' and len(per_str) > 1:
                    continue
                # Reattach decimal if present
                if dec_part is not None:
                    per_str = f'{per_str}.{dec_part}'
                try:
                    per_val = float(per_str)
                except ValueError:
                    continue
                common = COMMON_PER_UNIT.get(real_unit)
                is_common = common and per_val in common
                pref = COUNT_PREFERENCE.get(real_unit, 'high')
                tb = -count if pref == 'low' else count
                score = (100 if is_common else 10, tb)
                candidates.append((score, count, per_str, real_unit))

        # Strategy B: full number as single item (count=1)
        try:
            full_val = float(raw_full)
            common = COMMON_PER_UNIT.get(real_unit)
            if common and full_val in common:
                candidates.append(((100, 1), 1, raw_full, real_unit))
        except ValueError:
            pass

        # Strategy C: for decimals like "24.5L" → count=24, per=0.5
        if dec_part is not None:
            try:
                count_val = int(int_part)
                per_val = float(f'0.{dec_part}')
                if count_val in COMMON_PACK_COUNTS:
                    common = COMMON_PER_UNIT.get(real_unit)
                    is_common = common and per_val in common
                    if is_common:
                        pref = COUNT_PREFERENCE.get(real_unit, 'high')
                        tb = -count_val if pref == 'low' else count_val
                        candidates.append(((100, tb), count_val, f'0.{dec_part}', real_unit))
            except ValueError:
                pass

        if candidates:
            candidates.sort(reverse=True)
            _, count, per, u = candidates[0]
            return f'{count}/{per}{u}'

        return f'1/{raw_full}{real_unit}'

    # Pattern 3: "#10" cans — "6#10 HEINZ KETCHUP"
    m = re.match(r'^(\d+)\s*#10\b', desc, re.IGNORECASE)
    if m:
        return f'{m.group(1)}/#10'

    # Pattern 4: "N.N LB" with space (e.g., "42.5 LB")
    m = re.match(r'^(\d+\.?\d*)\s+LB\b', desc, re.IGNORECASE)
    if m:
        return f'1/{m.group(1)}LB'

    return None


def decode_farmart(desc):
    """Extract pack size from a Farm Art description."""
    # Ratio with unit: 6/1-QT, 4/2.5-LB, 24/1-LB
    m = re.search(r'(\d+)/(\d+\.?\d*)[\s-]*(GAL|LB|OZ|QT|DOZ|CT)', desc, re.IGNORECASE)
    if m:
        return f'{m.group(1)}/{m.group(2)}{m.group(3).upper()}'
    # Ratio without unit but with gal: 4/1gal, 4/gal
    m = re.search(r'(\d+)/(\d*)[\s-]*(gal)\b', desc, re.IGNORECASE)
    if m:
        per = m.group(2) or '1'
        return f'{m.group(1)}/{per}GAL'
    # N LB — space optional (handles '35LB' and '35 LB')
    m = re.search(r'(\d+\.?\d*)\s*LB\b', desc, re.IGNORECASE)
    if m:
        return f'1/{m.group(1)}LB'
    # N DOZ
    m = re.search(r'(\d+)[\s-]*DOZ\b', desc, re.IGNORECASE)
    if m:
        return f'{m.group(1)}DOZ'
    # N CT
    m = re.search(r'(\d+)\s*CT\b', desc, re.IGNORECASE)
    if m:
        return f'{m.group(1)}CT'
    # N BU (bunches)
    m = re.search(r'(\d+)\s*BU\b', desc, re.IGNORECASE)
    if m:
        return f'{m.group(1)}BU'
    # N HD (heads)
    m = re.search(r'(\d+)\s*HD\b', desc, re.IGNORECASE)
    if m:
        return f'{m.group(1)}HD'
    # N KG
    m = re.search(r'(\d+\.?\d*)\s*KG\b', desc, re.IGNORECASE)
    if m:
        return f'1/{m.group(1)}KG'
    return None


def decode_exceptional(desc):
    """Extract pack size from an Exceptional Foods description."""
    # NLB or N LB (case weight)
    m = re.search(r'(\d+\.?\d*)\s*LB\b', desc, re.IGNORECASE)
    if m:
        return f'1/{m.group(1)}LB'
    # N oz
    m = re.search(r'(\d+\.?\d*)\s*OZ\b', desc, re.IGNORECASE)
    if m:
        return f'1/{m.group(1)}OZ'
    # N dz or N DOZ
    m = re.search(r'(\d+)\s*(?:dz|DOZ)\b', desc, re.IGNORECASE)
    if m:
        return f'{m.group(1)}DOZ'
    # N CT
    m = re.search(r'(\d+)\s*CT\b', desc, re.IGNORECASE)
    if m:
        return f'{m.group(1)}CT'
    return None


def decode(desc, vendor=''):
    """Decode case size from any vendor's description."""
    if vendor == 'Sysco':
        return decode_sysco(desc)
    elif vendor == 'Farm Art':
        return decode_farmart(desc)
    elif vendor == 'Exceptional Foods':
        return decode_exceptional(desc)
    # Try all
    return decode_sysco(desc) or decode_farmart(desc) or decode_exceptional(desc)
