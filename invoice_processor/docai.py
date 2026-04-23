"""
Document AI invoice parser — structured extraction using Google Document AI.

Replaces the Vision API OCR + regex parser path with a pretrained invoice
processor that returns structured entities (vendor, date, line items).
Falls back gracefully: returns None on failure so batch.py can use the
legacy Vision+regex path.
"""
import os
import re
import tempfile
import time
from datetime import datetime

from google.api_core.client_options import ClientOptions
from google.cloud import documentai_v1 as documentai
from google.oauth2 import service_account

DOCAI_MAX_RETRIES = 3

from config import (
    CREDENTIALS_PATH,
    DOCAI_PROJECT_ID,
    DOCAI_LOCATION,
    DOCAI_PROCESSOR_ID,
)
from parser import _extract_case_size, detect_vendor, extract_date


# ── Vendor normalisation ────────────────────────────────────────────────────

_VENDOR_ALIASES = {
    "sysco":                   "Sysco",
    "colonial village":        "Colonial Village Meat Markets",
    "colonial meat":           "Colonial Village Meat Markets",
    "exceptional foods":       "Exceptional Foods",
    "exceptional":             "Exceptional Foods",
    "farmart":                 "Farm Art",
    "farm art":                "Farm Art",
    "pbm":                     "Philadelphia Bakery Merchants",
    "philadelphia bakery":     "Philadelphia Bakery Merchants",
    "delaware county linen":   "Delaware County Linen",
}


def _normalize_vendor(supplier_name: str) -> str:
    """Map a Document AI supplier_name entity to canonical vendor name."""
    if not supplier_name:
        return "Unknown"
    lower = supplier_name.lower()
    for alias, canonical in _VENDOR_ALIASES.items():
        if alias in lower:
            return canonical
    return "Unknown"


# ── MIME type lookup ────────────────────────────────────────────────────────

_MIME_MAP = {
    ".pdf":  "application/pdf",
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tiff": "image/tiff",
    ".tif":  "image/tiff",
}


# ── Document AI client ──────────────────────────────────────────────────────

_client = None


def _get_docai_client():
    global _client
    if _client is None:
        credentials = service_account.Credentials.from_service_account_file(
            CREDENTIALS_PATH,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        _client = documentai.DocumentProcessorServiceClient(
            credentials=credentials,
            client_options=ClientOptions(
                api_endpoint=f"{DOCAI_LOCATION}-documentai.googleapis.com"
            ),
        )
    return _client


# ── Entity helpers ──────────────────────────────────────────────────────────

def _get_entity_text(entity) -> str:
    """Return the mention_text of an entity, stripped."""
    return (entity.mention_text or "").strip()


def _get_child(entity, child_type: str):
    """Return the first child property matching child_type, or None."""
    for prop in entity.properties:
        if prop.type_ == child_type:
            return prop
    return None


def _parse_price(text: str) -> float | None:
    """Extract a numeric price from text like '$45.99', '45.99', '$ 12.30'."""
    if not text:
        return None
    cleaned = re.sub(r'[^\d.]', '', text)
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_quantity(text: str) -> float | None:
    """Extract a numeric quantity from text like '2', '1.5', '3 CS'."""
    if not text:
        return None
    m = re.search(r'(\d+\.?\d*)', text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


def _parse_docai_date(text: str) -> str:
    """Parse a date string from Document AI into YYYY-MM-DD format."""
    if not text:
        return ""
    # Try common formats Document AI may return
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y",
                "%Y-%m-%d", "%B %d, %Y", "%b %d, %Y",
                "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(text.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


# ── Sysco item code extraction ──────────────────────────────────────────────

_SYSCO_CODE_RE = re.compile(r'\b(\d{6,7})\b')


def _recover_description_from_raw(entity, raw_text: str) -> str:
    """
    When DocAI extracts a price entity but misses the description,
    look at the raw text near the entity's position to recover it.

    Strategy: take the region around the entity's text anchor and
    look for text that looks like a product description (contains
    3+ consecutive letters, not just numbers/prices/headers).
    """
    if not raw_text:
        return ""

    ts = entity.text_anchor.text_segments
    if not ts:
        return ""

    text_start = ts[0].start_index
    text_end = ts[0].end_index

    # Look 300 chars before the entity (description usually precedes price)
    start = max(0, text_start - 300)
    region = raw_text[start:text_end]

    # Split into lines and work backward from the entity position
    lines = [l.strip() for l in region.split('\n') if l.strip()]

    # Filter for description-like lines (3+ letters, not pure numbers/prices/headers)
    _SKIP_RE = re.compile(
        r'^[\d\s.,/$\-]+$'           # pure numbers/prices
        r'|^\d+\.\d{2,4}\s*$'        # standalone price
        r'|^T/WT='                    # weight total
        r'|GROUP\s*TOTAL'             # section total
        r'|SURCHARGE|CREDIT\s*CARD'   # surcharges
        r'|\*{3,}',                   # section headers
        re.IGNORECASE,
    )

    # Walk backward through lines to find the nearest description
    for line in reversed(lines):
        if _SKIP_RE.search(line):
            continue
        if not re.search(r'[A-Za-z]{3,}', line):
            continue
        if len(line) < 5:
            continue
        return line

    return ""


def _recover_price_from_raw(entity, raw_text: str, description: str = "") -> float | None:
    """
    When DocAI extracts a description entity but misses the price,
    search the raw text near the entity's position for a price.

    Strategy: look both AFTER and BEFORE the entity's text for standalone
    decimal numbers that look like prices (XX.XX format, $0.50-$500).
    Prefer prices found after the description (more common layout).
    """
    if not raw_text:
        return None

    ts = entity.text_anchor.text_segments
    if not ts:
        return None

    text_start = ts[0].start_index if ts else 0
    text_end = ts[-1].end_index if ts else 0

    price_re = re.compile(r'(?<!\d)(\d{1,4}\.\d{2})(?!\d)')

    # First: look 300 chars after the entity (prices usually follow descriptions)
    end = min(len(raw_text), text_end + 300)
    region_after = raw_text[text_end:end]
    for match in price_re.findall(region_after):
        try:
            price = float(match)
            if 0.50 <= price <= 500.0:
                return price
        except ValueError:
            continue

    # Second: look 200 chars before the entity (some formats put price first)
    start = max(0, text_start - 200)
    region_before = raw_text[start:text_start]
    # Walk backward — take the last price found before the entity
    matches_before = price_re.findall(region_before)
    for match in reversed(matches_before):
        try:
            price = float(match)
            if 0.50 <= price <= 500.0:
                return price
        except ValueError:
            continue

    return None


def _extract_sysco_code(description: str, raw_text: str = "",
                         text_start: int = 0, text_end: int = 0) -> str:
    """
    Extract a 6-7 digit Sysco item code from the description only.

    Only matches codes found directly in the description text — does NOT
    search nearby raw text, as that picks up codes from adjacent line items
    and produces ~95% wrong matches.
    """
    m = _SYSCO_CODE_RE.search(description)
    if m:
        return m.group(1)

    return ""


# ── Single-document processing ─────────────────────────────────────────────

def _docai_call_with_retry(client, processor_name: str,
                           file_content: bytes, mime_type: str):
    """Call DocAI with retry on transient errors (500, 503, UNAVAILABLE)."""
    raw_document = documentai.RawDocument(
        content=file_content, mime_type=mime_type
    )
    request = documentai.ProcessRequest(
        name=processor_name, raw_document=raw_document
    )
    for attempt in range(1, DOCAI_MAX_RETRIES + 1):
        try:
            return client.process_document(request=request, timeout=120)
        except Exception as e:
            err = str(e).lower()
            is_transient = any(k in err for k in ("500", "503", "unavailable",
                                                   "internal", "deadline"))
            if is_transient and attempt < DOCAI_MAX_RETRIES:
                wait = 2 ** attempt
                print(f"   [DocAI] Transient error (attempt {attempt}/{DOCAI_MAX_RETRIES}), "
                      f"retrying in {wait}s: {e}")
                time.sleep(wait)
            else:
                raise


def _process_single_document(file_content: bytes, mime_type: str,
                              client, processor_name: str) -> dict | None:
    """Process a single document through DocAI and extract structured data."""
    result = _docai_call_with_retry(client, processor_name,
                                     file_content, mime_type)
    document = result.document

    raw_text = document.text or ""
    vendor = "Unknown"
    invoice_date = ""

    for entity in document.entities:
        etype = entity.type_
        etext = _get_entity_text(entity)

        if etype == "supplier_name":
            vendor = _normalize_vendor(etext)
        elif etype == "invoice_date":
            invoice_date = _parse_docai_date(etext)

    # Fallback: use keyword detection on raw text if vendor not recognised
    if vendor == "Unknown" and raw_text:
        vendor = detect_vendor(raw_text)

    # Fallback: use regex date extraction on raw text
    if not invoice_date and raw_text:
        invoice_date = extract_date(raw_text)

    # ── Extract line items ──────────────────────────────────────────────
    #
    # DocAI often splits invoice lines into separate entities:
    #   - Description entities: have description, quantity, unit (left columns)
    #   - Price entities: have unit_price, amount (right columns)
    #
    # The pattern can be:
    #   DESC, PRICE, DESC, PRICE  (alternating 1:1)
    #   DESC, DESC, DESC, PRICE, PRICE, PRICE  (batched N:N)
    #   DESC+PRICE (combined in one entity)
    #
    # Strategy: collect runs of DESC entities and PRICE entities,
    # then zip them in order.

    items = []
    is_sysco = (vendor == "Sysco")

    line_entities = [e for e in document.entities if e.type_ == "line_item"]

    # First pass: classify each entity
    classified = []
    for entity in line_entities:
        desc_prop   = _get_child(entity, "line_item/description")
        price_prop  = _get_child(entity, "line_item/unit_price")
        amount_prop = _get_child(entity, "line_item/amount")
        qty_prop    = _get_child(entity, "line_item/quantity")
        unit_prop   = _get_child(entity, "line_item/unit")

        has_desc = bool(desc_prop and _get_entity_text(desc_prop))
        has_price = bool(price_prop or amount_prop)

        # Check if a "combined" entity's description is really just a number
        # (weight, qty, price). This happens on Exceptional Foods invoices where
        # DocAI reads the weight column as a "description". These should be
        # treated as price entities so they pair with the previous description.
        desc_text = _get_entity_text(desc_prop).strip() if desc_prop else ""
        desc_is_just_number = bool(
            has_desc and has_price
            and re.match(r'^[\d.,]+\s*$', desc_text)
        )

        if desc_is_just_number:
            etype = "price"
        elif has_desc and has_price:
            etype = "combined"
        elif has_desc:
            etype = "desc"
        else:
            etype = "price"

        classified.append({
            "entity": entity,
            "type": etype,
            "desc_prop": desc_prop,
            "price_prop": price_prop,
            "amount_prop": amount_prop,
            "qty_prop": qty_prop,
            "unit_prop": unit_prop,
        })

    # Second pass: collect runs and pair them
    # Walk through classified list, collecting desc runs and price runs
    desc_queue = []  # pending description entities waiting for prices

    def _build_item(desc_entry, price_entry=None):
        """Build an item dict from a desc entity and optional price entity."""
        entity = desc_entry["entity"]
        description = _get_entity_text(desc_entry["desc_prop"])

        # If description is empty, try to recover from raw text
        if not description:
            description = _recover_description_from_raw(entity, raw_text)

        quantity = None
        unit_of_measure = ""
        if desc_entry["qty_prop"]:
            quantity = _parse_quantity(_get_entity_text(desc_entry["qty_prop"]))
        if desc_entry["unit_prop"]:
            unit_of_measure = _get_entity_text(desc_entry["unit_prop"]).strip()

        unit_price = None
        needs_review = False

        # Try price from the desc entity itself (combined case)
        if desc_entry["price_prop"]:
            unit_price = _parse_price(_get_entity_text(desc_entry["price_prop"]))
        if unit_price is None and desc_entry["amount_prop"]:
            amount = _parse_price(_get_entity_text(desc_entry["amount_prop"]))
            if amount is not None and quantity and quantity > 0:
                unit_price = round(amount / quantity, 2)
            elif amount is not None:
                unit_price = amount
                needs_review = True

        # Try price from paired price entity
        if unit_price is None and price_entry:
            if price_entry["price_prop"]:
                unit_price = _parse_price(_get_entity_text(price_entry["price_prop"]))
            if unit_price is None and price_entry["amount_prop"]:
                amount = _parse_price(_get_entity_text(price_entry["amount_prop"]))
                if amount is not None and quantity and quantity > 0:
                    unit_price = round(amount / quantity, 2)
                elif amount is not None:
                    unit_price = amount
                    needs_review = True

        # Last resort: recover price from raw text near the entity
        if unit_price is None and raw_text and description:
            unit_price = _recover_price_from_raw(entity, raw_text, description)
            if unit_price is not None:
                needs_review = True

        # Case size: use quantity if available, fall back to regex
        case_size_raw = ""
        if quantity is not None and quantity > 0:
            case_size_raw = str(int(quantity)) if quantity == int(quantity) else str(quantity)
        else:
            case_size_raw = _extract_case_size(description)
            if not case_size_raw and raw_text:
                ts = entity.text_anchor.text_segments
                text_start = ts[0].start_index if ts else 0
                text_end = ts[0].end_index if ts else 0
                start = max(0, text_start - 100)
                end = min(len(raw_text), text_end + 100)
                case_size_raw = _extract_case_size(raw_text[start:end])

        if unit_price is not None:
            unit_price = float(unit_price)

        item = {
            "raw_description": description,
            "unit_price":      unit_price if unit_price and unit_price > 0 else None,
            "case_size_raw":   case_size_raw,
            "quantity":        quantity,
            "unit_of_measure": unit_of_measure,
        }

        if needs_review:
            item["needs_review"] = True

        if is_sysco:
            ts = entity.text_anchor.text_segments
            text_start = ts[0].start_index if ts else 0
            text_end = ts[0].end_index if ts else 0
            item["sysco_item_code"] = _extract_sysco_code(
                description, raw_text, text_start, text_end
            )

        return item

    for c in classified:
        if c["type"] == "combined":
            # Flush any pending descs without prices
            for d in desc_queue:
                items.append(_build_item(d))
            desc_queue.clear()
            # Process combined entity
            items.append(_build_item(c))

        elif c["type"] == "desc":
            desc_queue.append(c)

        elif c["type"] == "price":
            if desc_queue:
                # Pair with the oldest pending desc
                d = desc_queue.pop(0)
                items.append(_build_item(d, price_entry=c))
            else:
                # Orphan price entity — try to recover description from raw text
                entity = c["entity"]
                recovered_desc = _recover_description_from_raw(entity, raw_text)
                unit_price = None
                if c["price_prop"]:
                    unit_price = _parse_price(_get_entity_text(c["price_prop"]))
                if unit_price is None and c["amount_prop"]:
                    unit_price = _parse_price(_get_entity_text(c["amount_prop"]))

                if unit_price is not None and unit_price > 0:
                    case_size_raw = _extract_case_size(recovered_desc) if recovered_desc else ""
                    item = {
                        "raw_description": recovered_desc,
                        "unit_price":      float(unit_price),
                        "case_size_raw":   case_size_raw,
                        "quantity":        None,
                        "unit_of_measure": "",
                    }
                    if is_sysco:
                        ts = entity.text_anchor.text_segments
                        text_start = ts[0].start_index if ts else 0
                        text_end = ts[0].end_index if ts else 0
                        item["sysco_item_code"] = _extract_sysco_code(
                            recovered_desc, raw_text, text_start, text_end
                        )
                    items.append(item)

    # Flush remaining descs without prices
    for d in desc_queue:
        items.append(_build_item(d))
    desc_queue.clear()

    # ── Post-process: split merged descriptions ─────────────────────────
    # DocAI sometimes merges adjacent line items into one entity, producing
    # descriptions like "PRODUCT A\nPRODUCT B". Split these into separate
    # items when both halves look like independent product descriptions.
    #
    # BUT: many newlines are just OCR wrapping within one item, e.g.:
    #   "ONLY5 LB\nIMPFRSH CARROT BABY CUT" — qty prefix + description
    #   "MINOR\nBASE CHICKEN LOW SODI" — brand split across lines
    #   "LACROIX WATER SPARKLING\nGROUP" — description + section footer
    #
    # Only split when BOTH parts look like independent product descriptions:
    #   - Each has 3+ letter words
    #   - Each is >= 10 chars (short fragments are usually prefixes/suffixes)
    #   - First part is NOT just a quantity prefix (ONLY X LB, digits+unit, etc.)
    #   - Second part is NOT a junk suffix (GROUP, TOTAL, SUBSTITUTE, etc.)

    _QTY_PREFIX_RE = re.compile(
        r'^(?:ONLY\s*)?\d+[\d./]*\s*(?:LB|OZ|GAL|CT|CS|EA|DZ|KG|LTR|FL|PK|BG)?'
        r'(?:\s+[A-Z]{2,10})?\s*$',  # allow one trailing brand/unit word
        re.IGNORECASE,
    )
    _JUNK_SUFFIX_RE = re.compile(
        r'^(?:GROUP\b|TOTAL\b|SUBSTITUTE\b|OPEN:\s|CLOSE:\s|T/WT=|\*{2,})\s*',
        re.IGNORECASE,
    )

    def _looks_like_product(text: str) -> bool:
        """True if text looks like an independent product description."""
        text = text.strip()
        if len(text) < 10:
            return False
        if not re.search(r'[A-Za-z]{3,}', text):
            return False
        if _QTY_PREFIX_RE.match(text):
            return False
        if _JUNK_SUFFIX_RE.match(text):
            return False
        # Must have at least 2 letter-words to look like a real product
        words = re.findall(r'[A-Za-z]{2,}', text)
        return len(words) >= 2

    split_items = []
    for item in items:
        desc = item.get("raw_description", "")
        if "\n" not in desc:
            split_items.append(item)
            continue

        parts = [p.strip() for p in desc.split("\n") if p.strip()]
        product_parts = [p for p in parts if _looks_like_product(p)]

        if len(product_parts) >= 2:
            # True merge — split into separate items
            for i, part in enumerate(product_parts):
                new_item = {
                    "raw_description": part,
                    "unit_price":      item["unit_price"] if i == 0 else None,
                    "case_size_raw":   _extract_case_size(part),
                    "quantity":        item.get("quantity") if i == 0 else None,
                    "unit_of_measure": item.get("unit_of_measure", "") if i == 0 else "",
                }
                if is_sysco:
                    new_item["sysco_item_code"] = _extract_sysco_code(part)
                split_items.append(new_item)
        else:
            # Not a true merge — join parts back together (remove newlines)
            item["raw_description"] = " ".join(parts)
            split_items.append(item)

    return {
        "vendor": vendor,
        "invoice_date": invoice_date,
        "items": split_items,
    }


# ── PDF page splitting ─────────────────────────────────────────────────────

def _split_pdf_pages(file_path: str) -> list[str]:
    """Split a multi-page PDF into single-page temp files. Returns list of paths."""
    import fitz  # PyMuPDF

    doc = fitz.open(file_path)
    page_paths = []
    for i in range(len(doc)):
        single = fitz.open()
        single.insert_pdf(doc, from_page=i, to_page=i)
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        single.save(tmp.name)
        single.close()
        tmp.close()
        page_paths.append(tmp.name)
    doc.close()
    return page_paths


def _merge_results(results: list[dict]) -> dict:
    """Merge multiple per-page DocAI results into one."""
    vendor = "Unknown"
    invoice_date = ""
    all_items = []

    for r in results:
        if r["vendor"] != "Unknown":
            vendor = r["vendor"]
        if r["invoice_date"] and not invoice_date:
            invoice_date = r["invoice_date"]
        all_items.extend(r["items"])

    return {
        "vendor": vendor,
        "invoice_date": invoice_date,
        "items": all_items,
    }


# ── OCR-only entry point ───────────────────────────────────────────────────

def _extract_page_layout(document) -> list[dict]:
    """Strip the DocAI document down to per-page, per-token spatial records.
    Powers 2D spatial matching (see spatial_matcher.py) — each token has
    normalized x/y bounds plus char-offsets into document.text so downstream
    code can cross-reference spatial and textual views.

    Returns [{page_number, tokens: [{text, x_min, x_max, y_min, y_max,
    char_start, char_end}]}]. Empty list if layout data is missing."""
    raw_text = document.text or ""
    pages_out = []
    for page in document.pages:
        tokens = []
        for tok in page.tokens:
            layout = tok.layout
            ts_list = list(layout.text_anchor.text_segments)
            if not ts_list:
                continue
            ts = ts_list[0]
            start = int(ts.start_index) if ts.start_index else 0
            end = int(ts.end_index) if ts.end_index else 0
            text = raw_text[start:end].strip()
            vertices = list(layout.bounding_poly.normalized_vertices)
            if not vertices or not text:
                continue
            xs = [v.x for v in vertices]
            ys = [v.y for v in vertices]
            tokens.append({
                "text": text,
                "x_min": min(xs), "x_max": max(xs),
                "y_min": min(ys), "y_max": max(ys),
                "char_start": start, "char_end": end,
            })
        pages_out.append({
            "page_number": page.page_number,
            "tokens": tokens,
        })
    return pages_out


def _ocr_single_document(file_content: bytes, mime_type: str,
                          client, processor_name: str) -> dict | None:
    """Use DocAI for OCR only — extract raw text, vendor, and date.
    No line item entity extraction (that's handled by vendor-specific parsers).

    Also preserves per-token bounding-box layout (pages[].tokens[]) so the
    spatial matcher can pair anchors with descriptions by physical row,
    bypassing the column-read-order ambiguity of raw_text-based parsing."""
    result = _docai_call_with_retry(client, processor_name,
                                     file_content, mime_type)
    document = result.document

    raw_text = document.text or ""
    vendor = "Unknown"
    invoice_date = ""

    for entity in document.entities:
        etype = entity.type_
        etext = _get_entity_text(entity)

        if etype == "supplier_name":
            vendor = _normalize_vendor(etext)
        elif etype == "invoice_date":
            invoice_date = _parse_docai_date(etext)

    # Fallback: use keyword detection on raw text if vendor not recognised
    if vendor == "Unknown" and raw_text:
        vendor = detect_vendor(raw_text)

    # Fallback: use regex date extraction on raw text
    if not invoice_date and raw_text:
        invoice_date = extract_date(raw_text)

    return {
        "vendor": vendor,
        "invoice_date": invoice_date,
        "raw_text": raw_text,
        "pages": _extract_page_layout(document),
    }


def ocr_with_docai(file_path: str) -> dict | None:
    """
    Use Document AI for high-quality OCR, vendor detection, and date extraction.
    Returns {"vendor": str, "invoice_date": str, "raw_text": str}

    Line item extraction is NOT done here — the caller should pass raw_text
    to parser.parse_invoice() which uses vendor-specific parsers that understand
    column layouts.

    Results are cached locally by file content hash to avoid repeat API charges.
    Returns None if Document AI is not configured or the API call fails.
    """
    import ocr_cache
    cached = ocr_cache.get(file_path, "docai_ocr")
    if cached is not None and cached.get("pages"):
        print(f"   [DocAI OCR] Cache hit — skipping API call")
        return cached
    if cached is not None and not cached.get("pages"):
        # Pre-spatial cache entry — layout data absent. Re-OCR to populate
        # pages[], so Sysco spatial matching has what it needs.
        print(f"   [DocAI OCR] Cache hit missing layout data — re-OCR'ing")

    if not DOCAI_PROJECT_ID or not DOCAI_PROCESSOR_ID:
        return None

    try:
        client = _get_docai_client()
        processor_name = client.processor_path(
            DOCAI_PROJECT_ID, DOCAI_LOCATION, DOCAI_PROCESSOR_ID
        )

        ext = os.path.splitext(file_path)[1].lower()
        mime_type = _MIME_MAP.get(ext, "application/pdf")

        with open(file_path, "rb") as f:
            file_content = f.read()

        result = _ocr_single_document(file_content, mime_type,
                                       client, processor_name)

        # Handle multi-page PDFs
        if result and not result.get("raw_text"):
            ocr_cache.put(file_path, "docai_ocr", result)
            return result

        ocr_cache.put(file_path, "docai_ocr", result)
        return result

    except Exception as e:
        error_str = str(e)

        if "PAGE_LIMIT_EXCEEDED" in error_str and ext == ".pdf":
            print(f"   [DocAI OCR] Page limit exceeded — splitting PDF into pages...")
            page_paths = []
            try:
                page_paths = _split_pdf_pages(file_path)
                print(f"   [DocAI OCR] Processing {len(page_paths)} pages individually...")
                all_text = []
                all_pages = []
                vendor = "Unknown"
                invoice_date = ""
                char_offset = 0
                for i, pp in enumerate(page_paths):
                    try:
                        with open(pp, "rb") as f:
                            page_content = f.read()
                        r = _ocr_single_document(page_content, "application/pdf",
                                                  client, processor_name)
                        if r:
                            page_text = r.get("raw_text", "")
                            if page_text:
                                all_text.append(page_text)
                            # Carry forward per-page layout, renumbering pages
                            # and shifting char offsets so they index into the
                            # merged raw_text ("\n".join(all_text)).
                            for pg in r.get("pages", []) or []:
                                shifted_tokens = []
                                for tok in pg.get("tokens", []):
                                    shifted_tokens.append({
                                        **tok,
                                        "char_start": tok["char_start"] + char_offset,
                                        "char_end": tok["char_end"] + char_offset,
                                    })
                                all_pages.append({
                                    "page_number": len(all_pages) + 1,
                                    "tokens": shifted_tokens,
                                })
                            # Account for the "\n" separator added by join
                            char_offset += len(page_text) + (1 if page_text else 0)
                            if r["vendor"] != "Unknown" and vendor == "Unknown":
                                vendor = r["vendor"]
                            if r["invoice_date"] and not invoice_date:
                                invoice_date = r["invoice_date"]
                    except Exception as page_err:
                        print(f"     Page {i+1}: error — {page_err}")
                merged_result = {
                    "vendor": vendor,
                    "invoice_date": invoice_date,
                    "raw_text": "\n".join(all_text),
                    "pages": all_pages,
                }
                ocr_cache.put(file_path, "docai_ocr", merged_result)
                return merged_result
            finally:
                for pp in page_paths:
                    if os.path.exists(pp):
                        os.remove(pp)

        print(f"   [DocAI OCR] API error: {e}")
        return None


# ── Legacy entity-based entry point (kept for reference) ──────────────────

def parse_with_docai(file_path: str) -> dict | None:
    """
    Process an invoice with Document AI and return structured data.

    Returns the same dict format as parser.parse_invoice():
        {"vendor": str, "invoice_date": str, "items": [list of dicts]}

    Each item dict has: raw_description, unit_price, case_size_raw,
    and optionally sysco_item_code.

    Results are cached locally by file content hash to avoid repeat API charges.
    Returns None if Document AI is not configured or the API call fails,
    signalling the caller to fall back to the Vision+regex path.
    """
    import ocr_cache
    cached = ocr_cache.get(file_path, "docai_entities")
    if cached is not None:
        print(f"   [DocAI] Cache hit — skipping API call")
        return cached

    if not DOCAI_PROJECT_ID or not DOCAI_PROCESSOR_ID:
        return None

    try:
        client = _get_docai_client()
        processor_name = client.processor_path(
            DOCAI_PROJECT_ID, DOCAI_LOCATION, DOCAI_PROCESSOR_ID
        )

        ext = os.path.splitext(file_path)[1].lower()
        mime_type = _MIME_MAP.get(ext, "application/pdf")

        with open(file_path, "rb") as f:
            file_content = f.read()

        result = _process_single_document(file_content, mime_type,
                                         client, processor_name)
        ocr_cache.put(file_path, "docai_entities", result)
        return result

    except Exception as e:
        error_str = str(e)

        # If page limit exceeded on a PDF, split and process per-page
        if "PAGE_LIMIT_EXCEEDED" in error_str and ext == ".pdf":
            print(f"   [DocAI] Page limit exceeded — splitting PDF into pages...")
            page_paths = []
            try:
                page_paths = _split_pdf_pages(file_path)
                print(f"   [DocAI] Processing {len(page_paths)} pages individually...")
                page_results = []
                for i, pp in enumerate(page_paths):
                    try:
                        with open(pp, "rb") as f:
                            page_content = f.read()
                        r = _process_single_document(page_content, "application/pdf",
                                                      client, processor_name)
                        if r and r.get("items"):
                            page_results.append(r)
                            print(f"     Page {i+1}: {len(r['items'])} items")
                        else:
                            print(f"     Page {i+1}: 0 items")
                    except Exception as page_err:
                        print(f"     Page {i+1}: error — {page_err}")
                if page_results:
                    merged = _merge_results(page_results)
                    print(f"   [DocAI] Merged: {len(merged['items'])} total items from {len(page_results)} pages")
                    ocr_cache.put(file_path, "docai_entities", merged)
                    return merged
                return None
            finally:
                for pp in page_paths:
                    if os.path.exists(pp):
                        os.remove(pp)

        print(f"   [DocAI] API error: {e}")
        return None
