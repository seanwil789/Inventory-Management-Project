from django.db import models


class Vendor(models.Model):
    name = models.CharField(max_length=120, unique=True)

    def __str__(self):
        return self.name


class Account(models.Model):
    """Department / cost-center for invoice attribution (Sean 2026-06-08).

    Partitions spend so non-food departments (Operations cleaning supplies,
    Coffee/Concessions) leave the FOOD budget while still informing prices.
    Maps 1:1 to a QuickBooks Class so accounting gets pre-coded per-account
    data. See PACKAGE_department_accounts.md.

    Two flags, each serving a downstream consumer (schema-encodes-operating-model):
      - is_default:     account that untagged invoices fall to (Food/Kitchen).
      - is_food_budget: included in food-budget / COGS aggregations (Food only
                        today). The account-aware consumers filter on this.

    The price/mapping layer is account-BLIND — attribution never touches the
    catalog. Only budget/COGS consumers filter by account.
    """
    name           = models.CharField(max_length=80, unique=True)
    qb_class       = models.CharField(
        max_length=40, blank=True,
        help_text="QuickBooks Class name/code this account maps to (1:1).")
    is_default     = models.BooleanField(
        default=False,
        help_text="Account untagged invoices fall to (Food/Kitchen). Exactly one.")
    is_food_budget = models.BooleanField(
        default=False,
        help_text="Included in food-budget / COGS aggregations. Account-aware "
                  "consumers filter on this; account-blind (price/mapping) ignore it.")
    sort_order     = models.IntegerField(default=0)

    class Meta:
        ordering = ['sort_order', 'name']

    def __str__(self):
        return self.name

    @classmethod
    def default(cls):
        """The default (Food/Kitchen) account, or None if not seeded yet.
        Consumers treat a NULL ILI.account as this default."""
        return cls.objects.filter(is_default=True).order_by('sort_order').first()


class Product(models.Model):
    """Canonical product — mirrors Item Mapping col F + category columns."""
    canonical_name       = models.CharField(max_length=200, unique=True)
    category             = models.CharField(max_length=100, blank=True)
    primary_descriptor   = models.CharField(max_length=100, blank=True)
    secondary_descriptor = models.CharField(max_length=100, blank=True)
    default_case_size    = models.CharField(
        max_length=30, blank=True,
        help_text="Canonical pack size for this product (e.g. '4/1GAL'). "
                  "Fallback when an InvoiceLineItem has no case_size from "
                  "its raw description. Inferred from mode of historical "
                  "invoices by `infer_product_default_case_sizes`.",
    )
    prep_state           = models.CharField(
        max_length=30, blank=True,
        help_text="Per-category form descriptor. Cheese: Whole/Shredded/"
                  "Sliced/Loaf/Balls/Wedge/Crumbled. Captures price-differential "
                  "between bought-prepped and prepped-in-house forms. "
                  "Will phase out as in-house prep replaces purchased pre-prep.",
    )
    # ── Structured inventory schema (Phase 1, 2026-05-02) ─────────────────
    # Per Sean's `feedback_inventory_count_classes.md` 2-class methodology
    # (Weighed vs Counted-with-Fraction) extended to 3 classes for the
    # cost-calc / sheet F+G logic.
    inventory_class = models.CharField(
        max_length=30, blank=True,
        choices=[
            ('', '— unset —'),
            ('weighed', 'Weighed (proteins, cheese — $/lb pricing, scale required)'),
            ('counted_with_weight', 'Counted-with-weight (uniform packs — count blocks/tubs/bottles)'),
            ('counted_with_volume', 'Counted-with-volume (gallons/quarts/pints — count containers)'),
        ],
        help_text="Inventory class — drives sheet F+G column logic + recipe "
                  "cost dispatch. Weighed: F=total lb, G='#'. "
                  "Counted-with-weight: F=count, G='1# Print' / '5# Tub' / etc. "
                  "Counted-with-volume: F=count, G='Gal' / 'Qt' / 'Pt'. "
                  "Future: type-check fuzzy mapper (reject yogurt→shrimp class mismatch).",
    )
    inventory_unit_descriptor = models.CharField(
        max_length=60, blank=True,
        help_text="Human-readable unit descriptor that lands in sheet col G. "
                  "Examples: '1# Print' / '5# Tub' / '5.3 oz Patty' / "
                  "'Half-pint Clamshell' / 'Gal' / 'Btl' / 'Ea'. "
                  "Replaces sheet col G's ad-hoc strings with controlled values.",
    )

    def __str__(self):
        return self.canonical_name


class ProductMapping(models.Model):
    """
    Maps a raw vendor description (or SUPC code) to a canonical Product.
    Mirrors Item Mapping rows — one row per vendor+description variant.
    """
    vendor      = models.ForeignKey(Vendor, null=True, blank=True, on_delete=models.SET_NULL)
    description = models.CharField(max_length=500)           # raw OCR / CSV description
    supc        = models.CharField(max_length=20, blank=True, db_index=True)
    product     = models.ForeignKey(Product, null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        unique_together = [('vendor', 'description')]

    def __str__(self):
        return f"{self.description} → {self.product}"


class ProductMappingProposal(models.Model):
    """
    Pending mapping suggestion awaiting human review. Replaces the
    Sheets-based Mapping Review tab workflow with a DB queue that the
    Django /mapping-review/ UI can render and approve/reject.

    Two surfacing paths feed this queue:
      1. Mapper quarantine (Phase 2 of mapper safety) — when db_write
         encounters a fuzzy-tier match (vendor_fuzzy / fuzzy /
         stripped_fuzzy), it does NOT auto-attach the FK to the ILI.
         Instead it sets product=NULL + match_confidence='<tier>_pending'
         and creates a proposal row. The fuzzy match never silently
         becomes ground truth.
      2. discover_unmapped scan — periodic batch finds ILI rows still
         unmapped after N occurrences, fuzzy-matches against existing
         canonicals, and proposes the best fits for human review.

    On approval: the proposal's suggested_product is committed to
    ProductMapping (so future invoices map automatically) AND attached
    as the FK to all matching ILI rows (with match_confidence flipped to
    'manual_review' for audit trail).

    On rejection: status flips to 'rejected'; no DB writes elsewhere.
    Re-suggestion of the same (vendor, description) pair is suppressed
    by the negative_matches.json cache.
    """
    STATUS_CHOICES = [
        ('pending',  'Pending Review'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ]
    SOURCE_CHOICES = [
        ('mapper_quarantine', 'Mapper Quarantine (fuzzy held at write)'),
        ('discover_unmapped', 'Discover Unmapped Scan'),
        ('drift_audit', 'Canonical Drift Audit (PM re-evaluation)'),
        ('suspect_audit', 'Suspect-Mappings Audit (zero token overlap)'),
        ('supc_recovery', 'Sysco SUPC Cross-Cache Recovery'),
    ]
    vendor              = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name='mapping_proposals')
    raw_description     = models.CharField(max_length=500)
    suggested_product   = models.ForeignKey(
        Product, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='mapping_proposals',
        help_text="The mapper's best guess. Null when the human is inventing a new canonical.",
    )
    score               = models.IntegerField(null=True, blank=True,
                                               help_text="Fuzzy score 0-100 from the source scorer.")
    confidence_tier     = models.CharField(max_length=30, blank=True,
                                            help_text="vendor_fuzzy / fuzzy / stripped_fuzzy / etc.")
    source              = models.CharField(max_length=30, choices=SOURCE_CHOICES,
                                            default='mapper_quarantine')
    status              = models.CharField(max_length=15, choices=STATUS_CHOICES,
                                            default='pending', db_index=True)
    notes               = models.TextField(blank=True)
    # Sean 2026-05-02: structured rejection reason. Free-text `notes` are
    # for context; `reject_reason` is the categorical signal so audit +
    # populate cmds can filter by reason class. Empty string when not
    # yet rejected OR rejected without a reason given.
    REJECT_REASON_CHOICES = [
        ('', '— none —'),
        ('wrong_canonical', 'Wrong canonical (suggestion is the wrong product)'),
        ('not_a_product', 'Not a product (boilerplate / fee / line noise)'),
        ('typo_or_garble', 'Typo or OCR garble in raw description'),
        ('different_class', 'Different inventory class (weighed vs counted)'),
        ('belongs_elsewhere', 'Belongs in a different canonical that exists'),
        ('needs_new_canonical', 'Needs a new canonical (none exists yet)'),
        ('other', 'Other (see notes)'),
    ]
    reject_reason       = models.CharField(
        max_length=30, blank=True, choices=REJECT_REASON_CHOICES,
        db_index=True,
        help_text="Categorical reason for the rejection — drives audit "
                  "filtering + future quality metrics.",
    )
    created_at          = models.DateTimeField(auto_now_add=True)
    reviewed_at         = models.DateTimeField(null=True, blank=True)
    reviewed_by         = models.ForeignKey(
        'auth.User', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='mapping_proposals_reviewed',
    )
    # Suggestion-vs-final tracking — populated only on the create_and_approve
    # flow, where derive_canonical_suggestion offers an auto-cleaned starting
    # point. Comparing the two over time lets us refine the derivation logic
    # from the corpus of human edits.
    suggested_canonical_text = models.CharField(
        max_length=200, blank=True,
        help_text="Auto-derived canonical name suggestion shown in the create form.",
    )
    final_canonical_text     = models.CharField(
        max_length=200, blank=True,
        help_text="Canonical name the reviewer actually saved (may differ from suggested).",
    )

    class Meta:
        # Sean 2026-05-02: previously `unique_together(vendor, raw_description)`
        # which conflated "this raw has been seen" with "this proposal has been
        # decided." Sean's unification rule: items with no canonical should
        # resurface; items with canonical only re-review on drift_audit
        # trigger. Different sources can coexist for the same raw — fuzzy
        # quarantine + drift audit can both propose without colliding.
        unique_together = [('vendor', 'raw_description', 'source', 'suggested_product')]
        indexes = [
            models.Index(fields=['status', '-created_at']),
            models.Index(fields=['vendor', 'raw_description']),
        ]
        ordering = ['-created_at']

    def __str__(self):
        suggested = self.suggested_product.canonical_name if self.suggested_product else '(none)'
        return f"[{self.status}] {self.vendor.name if self.vendor else '?'} | {self.raw_description[:40]} → {suggested}"

    def approve(self, *, product=None, reviewer=None, notes: str = ''):
        """Apply this proposal:
          1. Update the proposal row (status='approved', suggested_product
             possibly overridden, reviewed_at set, reviewed_by set).
          2. Upsert ProductMapping(vendor, description) → product so future
             invoices auto-resolve at vendor_exact.
          3. Attach the FK to ALL existing ILI rows with the same
             (vendor, raw_description) and bump their match_confidence to
             'manual_review' for audit trail.

        Returns dict: {'ili_updated': int, 'product_mapping': ProductMapping}
        """
        from django.utils import timezone
        # Allow caller to override the suggested_product (Sean disagrees with mapper)
        final_product = product or self.suggested_product
        if final_product is None:
            raise ValueError('Cannot approve a proposal with no product set')

        # 1. Update proposal
        self.status = 'approved'
        self.suggested_product = final_product
        self.reviewed_at = timezone.now()
        self.reviewed_by = reviewer
        if notes:
            self.notes = (self.notes + '\n' if self.notes else '') + notes
        self.save()

        # 2. Upsert ProductMapping
        pm, _ = ProductMapping.objects.update_or_create(
            vendor=self.vendor,
            description=self.raw_description,
            defaults={'product': final_product},
        )

        # 3. Backfill ILI rows (all historical with same vendor + raw_desc)
        ili_updated = InvoiceLineItem.objects.filter(
            vendor=self.vendor,
            raw_description=self.raw_description,
        ).update(
            product=final_product,
            match_confidence='manual_review',
        )

        return {'ili_updated': ili_updated, 'product_mapping': pm}

    @classmethod
    def get_or_create_dedup(cls, *, vendor, raw_description, suggested_product,
                            source: str, defaults: dict | None = None):
        """Cross-source dedup (Sean 2026-05-02): when ANY existing PMP
        already proposes this `(vendor, raw_description, suggested_product)`
        triple — regardless of source — return the existing one with a
        source-convergence marker appended to its notes. Otherwise
        create a new PMP with the given source.

        Returns (proposal, created, converged) tuple:
          proposal:  the PMP (existing or new)
          created:   True if newly created, False if reused
          converged: True if reused AND a different source previously
                     proposed this same target (multi-source signal)

        Source markers in notes use compact tags: [mq], [du], [da] for
        mapper_quarantine, discover_unmapped, drift_audit.
        """
        defaults = dict(defaults or {})
        marker_map = {
            'mapper_quarantine': '[mq]',
            'discover_unmapped': '[du]',
            'drift_audit': '[da]',
            'suspect_audit': '[sa]',
            'supc_recovery': '[sr]',
        }
        marker = marker_map.get(source, f'[{source[:2]}]')

        existing = cls.objects.filter(
            vendor=vendor,
            raw_description=raw_description,
            suggested_product=suggested_product,
        ).order_by('id').first()

        if existing is not None:
            converged = (existing.source != source
                         and marker not in (existing.notes or ''))
            if converged:
                existing.notes = (existing.notes + ' ' if existing.notes else '') + marker
                existing.save(update_fields=['notes'])
            return existing, False, converged

        # Create new PMP with originating source marker pre-seeded in notes
        existing_notes = defaults.pop('notes', '')
        new_notes = (existing_notes + ' ' if existing_notes else '') + marker
        new = cls.objects.create(
            vendor=vendor,
            raw_description=raw_description,
            suggested_product=suggested_product,
            source=source,
            notes=new_notes,
            **defaults,
        )
        return new, True, False

    def converged_sources(self) -> set[str]:
        """Read source markers stamped in notes back into a source set.
        Includes the originating source so the count reflects all paths
        that converged on this (vendor, raw, suggested) triple."""
        markers = {'[mq]', '[du]', '[da]', '[sa]', '[sr]'}
        found = {m for m in markers if m in (self.notes or '')}
        # Always include the originating source
        marker_map = {
            'mapper_quarantine': '[mq]',
            'discover_unmapped': '[du]',
            'drift_audit': '[da]',
            'suspect_audit': '[sa]',
            'supc_recovery': '[sr]',
        }
        if self.source in marker_map:
            found.add(marker_map[self.source])
        return found

    def reject(self, *, reviewer=None, notes: str = '', reason: str = ''):
        """Mark this proposal rejected. `reason` is a categorical key
        from REJECT_REASON_CHOICES; `notes` is free-text supplementary
        context. The audit + populate cmds filter rejected proposals
        by reason — e.g. 'not_a_product' rejections feed the boilerplate
        guard regex; 'wrong_canonical' rejections inform the drift
        audit's negative-pair set."""
        from django.utils import timezone
        self.status = 'rejected'
        self.reviewed_at = timezone.now()
        self.reviewed_by = reviewer
        if reason:
            valid = {k for k, _ in self.REJECT_REASON_CHOICES}
            if reason in valid:
                self.reject_reason = reason
        if notes:
            self.notes = (self.notes + '\n' if self.notes else '') + notes
        self.save()


class InvoiceLineItem(models.Model):
    """
    Single line item from a processed invoice.
    Replaces the Data Sheets transaction log.
    """
    CONFIDENCE_CHOICES = [
        ('code', 'SUPC Code Match'),
        ('exact', 'Exact Description Match'),
        ('vendor_exact', 'Vendor-Scoped Exact'),
        ('vendor_fuzzy', 'Vendor-Scoped Fuzzy'),
        ('fuzzy', 'Fuzzy Match'),
        ('stripped_fuzzy', 'Stripped Prefix Fuzzy'),
        ('subset_match', 'Subset Match (canonical tokens ⊆ raw)'),
        ('keyword_batch', 'Keyword Batch (Human)'),
        ('manual_review', 'Manual Review (Human)'),
        ('auto_repoint', 'Auto-Repointed (live mapper produced different canonical)'),
        ('non_product', 'Non-Product (surcharge/fee/credit)'),
        ('unmatched', 'Unmatched'),
        ('unmatched_drift', 'Unmatched (Sheet/DB Drift)'),
        # Sean 2026-05-02: parser-garble tag set when Sean rejects a
        # PMP with reason='typo_or_garble'. Drops the ILI out of
        # /mapping-review/ unresolved filter (no point re-surfacing
        # garbled raws) AND into the audit_parser_garbles queue
        # which surfaces parser bugs for diagnosis.
        ('unmatched_garbled', 'Unmatched (Parser Garble)'),
        # Phase 3e/3f boundary guards — db_write rejects FK attach when
        # raw line item's class signal disagrees with Product.inventory_class
        # (volume vs weighed mismatch). Tags the ILI without an FK so the
        # row is visible in audits.
        ('unmatched_class_mismatch', 'Unmatched (Class Mismatch Guard)'),
        # cleanup_canonical_conflation detach — when raw lacks the
        # canonical's keep-tokens, FK is detached + tagged here so the
        # row drops from /mapping-review/ unresolved without re-surfacing
        # the same conflated suggestion.
        ('unmatched_repointed', 'Unmatched (Repointed by Cleanup)'),
        # Fuzzy-quarantine pending tags — db_write quarantines fuzzy-tier
        # hits with FK detached; ILI gets <tier>_pending while the PMP
        # awaits human review in /mapping-review/.
        ('vendor_fuzzy_pending', 'Vendor Fuzzy (Quarantined, Pending Review)'),
        ('subset_match_pending', 'Subset Match (Quarantined, Pending Review)'),
        ('stripped_fuzzy_pending', 'Stripped Fuzzy (Quarantined, Pending Review)'),
    ]

    vendor          = models.ForeignKey(Vendor, null=True, blank=True, on_delete=models.SET_NULL)
    product         = models.ForeignKey(Product, null=True, blank=True, on_delete=models.SET_NULL)
    # Department / cost-center attribution (Sean 2026-06-08). NULL = the default
    # (Food/Kitchen) account — backfilled on existing rows, treated as Food by
    # account-aware consumers. Set per-invoice (bulk) in the UI; line-level
    # supported for the rare mixed invoice. ACCOUNT-BLIND consumers (price,
    # mapping, VendorPriceList, alerts) ignore this field entirely.
    account         = models.ForeignKey(
        'Account', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='line_items',
        help_text="Department/cost-center this line is attributed to. "
                  "NULL = Food/Kitchen default. Drives budget/COGS filtering only.")
    raw_description = models.CharField(max_length=500, blank=True)  # fallback if unmatched
    unit_price      = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    extended_amount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Line total (qty × unit_price) as printed on the invoice. "
                  "For Sysco, typically equals unit_price. For Farm Art and "
                  "other per-unit priced vendors, this captures the qty multiplier.")
    price_per_pound = models.DecimalField(
        max_digits=10, decimal_places=4, null=True, blank=True,
        help_text="Per-pound price computed directly by the parser. Populated "
                  "for Sysco catch-weight (MEATS/POULTRY/SEAFOOD) and all "
                  "Exceptional Foods rows where the vendor bills per-lb. Null "
                  "elsewhere (Farm Art, PBM, Delaware Linen, Colonial). "
                  "Consumers should prefer this field over reverse-engineering "
                  "$/lb from unit_price + case_size.")
    case_size       = models.CharField(
        max_length=100, blank=True,
        help_text="LEGACY string field — preserved for backward compat + audit. "
                  "Holds 6+ semantic shapes (vendor weight, pack format, "
                  "merged count×size, volume container count, catch-weight, "
                  "U/M token, range). Consumers should prefer the structured "
                  "fields below (quantity, purchase_uom, case_pack_*) when "
                  "populated. Deprecation target: post-Phase-3 of structured "
                  "schema migration.",
    )
    # ── Structured invoice-line schema (Phase 1, 2026-05-02) ──────────────
    # Replaces overloaded `case_size` string with typed fields. Sources from
    # spatial_matcher (already extracts qty for PBM/Exc/FA/Del) + parser
    # (catches case-pack tokens). Threaded through db_write.
    quantity = models.DecimalField(
        max_digits=10, decimal_places=3, null=True, blank=True,
        help_text="Quantity ordered/shipped from the invoice line. "
                  "Exceptional catch-weight = actual shipped lbs (e.g. 8.56). "
                  "Farm Art / PBM / Delaware = qty in vendor's U/M (often 1.0 CASE). "
                  "Sysco = always 1 (one case per line). NULL when parser couldn't extract.",
    )
    purchase_uom = models.CharField(
        max_length=10, blank=True,
        help_text="Vendor's U/M column verbatim. EACH / CASE / LB / DZ / etc. "
                  "Free varchar — controlled vocabulary will emerge from audit "
                  "after backfill. Distinguishes per-piece vs per-case pricing "
                  "for Farm Art (Celery 3 stalks @ $2.60/EACH = $7.72 vs $7.72/case).",
    )
    case_pack_count = models.IntegerField(
        null=True, blank=True,
        help_text="Units per case (e.g. 60 patties/case for Burgers 60/5.3OZ). "
                  "First half of vendor's pack-size column. NULL when not applicable "
                  "(catch-weight rows, single-unit bulk, etc.).",
    )
    case_pack_unit_size = models.DecimalField(
        max_digits=10, decimal_places=3, null=True, blank=True,
        help_text="Size of each unit (e.g. 5.3 oz for 60/5.3OZ Burgers). "
                  "Decimal-tolerant (parser caps at 48 missed 5.3oz patties; "
                  "case_size_decoder handles up to 2000+ with decimals).",
    )
    case_pack_unit_uom = models.CharField(
        max_length=10, blank=True,
        help_text="Unit-of-size for each pack member: OZ / LB / CT / PT / DZ / GAL.",
    )
    case_total_weight_lb = models.DecimalField(
        max_digits=10, decimal_places=3, null=True, blank=True,
        help_text="Total case weight in lb — derived canonical for $/lb math. "
                  "= case_pack_count × case_pack_unit_size (with unit conversion). "
                  "Replaces the parse-string-and-pray flow in synergy_sync.calc_price_per_lb.",
    )
    # Per-piece count grade for weighed products (bacon, shrimp, scallops).
    # Sean 2026-05-02: bacon is weighed for COUNT purposes — recipe says
    # "2 strips bacon", we need (10+14)/2 = 12 strips/lb to compute cost.
    # Range comes from raw_description tokens like "10/14", "21/25", "26/30".
    count_per_lb_low = models.IntegerField(
        null=True, blank=True,
        help_text="Low end of per-lb count grade (e.g. 10 for bacon '10/14'). "
                  "Used by recipe cost calc for per-piece pricing. "
                  "Extracted from raw_description by per-vendor parsers.",
    )
    count_per_lb_high = models.IntegerField(
        null=True, blank=True,
        help_text="High end of per-lb count grade (e.g. 14 for bacon '10/14'). "
                  "Recipe cost = ($/lb / avg(low,high)) × strips_called_for.",
    )
    invoice_date    = models.DateField(null=True, blank=True)
    invoice_number  = models.CharField(
        max_length=20, blank=True, db_index=True,
        help_text="Vendor-extracted invoice number (e.g. Sysco '775687424', "
                  "Farm Art '1654186'). Stable across re-photo/reprocess cycles. "
                  "Phase 4c (Sean 2026-05-10): primary dedup key, replacing "
                  "source_file-based dedup. Empty when extraction unreliable "
                  "(Colonial; some PBM/Delaware) — falls back to legacy keys.",
    )
    source_file     = models.CharField(max_length=255, blank=True)  # original filename
    match_confidence = models.CharField(max_length=30, blank=True, choices=CONFIDENCE_CHOICES)
    match_score     = models.IntegerField(null=True, blank=True)  # 0-100 fuzzy score
    price_flagged   = models.BooleanField(default=False)  # True if price anomaly detected (historical drift)
    # B6 (project_parser_accuracy_goal.md): line-math validation flag.
    # Set at parse time by `invoice_processor/line_math.py` when
    # qty × price ≠ extended beyond tolerance (catch-weight aware: uses
    # price_per_pound when populated, unit_price otherwise).
    # Downstream consumers (synergy_sync, cost_utils, /price-alerts/,
    # /category-spend/, /cogs/) filter math_flagged=True from aggregations
    # so poisoned rows don't distort dashboards. Per Trust LAW.
    math_flagged    = models.BooleanField(default=False, db_index=True)
    # L1 Phase 1.2 (invoice reconciliation): True once Sean has edited
    # this row through `/invoices/<id>/lines/<id>/edit/`. Distinguishes
    # auto-extracted from human-corrected for downstream trust scoring.
    # Edit history captured in InvoiceLineEdit (FK). Per Trust LAW.
    user_edited     = models.BooleanField(default=False, db_index=True)
    section_hint    = models.CharField(
        max_length=60, blank=True, db_index=True,
        help_text="Section header from the invoice (e.g. 'DAIRY', "
                  "'CHEMICAL & JANITORIAL'). Used to categorize unknown-code "
                  "rows where the OCR dropped the description column.",
    )
    # Self-healing raw descriptions architecture (`project_self_healing_raw_descriptions.md`):
    # canonical SKU identity decoupled from historical price evidence. The FK identifies
    # WHICH vendor catalog SKU this transaction is for; the price/qty/ext fields above
    # remain immutable historical records of what was charged (`feedback_event_driven_pricing.md`
    # LAW). Null when no canonical match exists yet — surfaces in mapping-review for
    # human resolution. ON DELETE SET NULL preserves history if a catalog SKU is removed.
    canonical_vendor_pricelist = models.ForeignKey(
        'VendorPriceList', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='attached_invoice_lines',
        help_text="Canonical vendor SKU this line item represents. "
                  "Identity pointer; does NOT replace historical price fields.",
    )
    # Phase 4f (2026-05-17): SUPC/vendor-item-code captured at parse time.
    # Decouples dedup from raw_description, which is unstable across parser
    # versions (token-clustering improvements change which tokens land in
    # the desc). The SUPC is the stable identity field on the invoice line:
    # printed in a known column, parsed by anchor recognition, doesn't
    # depend on neighboring-row token assignment. db_write prefers
    # (vendor, invoice_number, vendor_item_code) as the primary dedup key
    # when populated. Sysco populates; other vendors may or may not.
    vendor_item_code = models.CharField(
        max_length=32, blank=True, db_index=True,
        help_text="Vendor's catalog code for this line item (Sysco SUPC, "
                  "Farm Art item code, etc.). Stable across parser versions; "
                  "preferred dedup key over raw_description.",
    )
    imported_at     = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['vendor', 'invoice_date']),
            models.Index(fields=['product', 'invoice_date']),
            models.Index(fields=['canonical_vendor_pricelist', 'invoice_date']),
        ]

    def __str__(self):
        name = self.product.canonical_name if self.product else self.raw_description
        return f"{self.invoice_date} | {self.vendor} | {name} | ${self.unit_price}"

    @property
    def effective_case_size(self) -> str:
        """Return this line's case_size, falling back to the linked product's
        default_case_size when the invoice didn't specify one. Consumers
        doing per-unit math should prefer this over the raw case_size field."""
        return self.case_size or (self.product.default_case_size if self.product else '')


ASSIGNEE_CHOICES = [
    ('sean', 'Sean'),
    ('albert', 'Albert'),
]


PROTEIN_CHOICES = [
    ('beef',    'Beef'),
    ('chicken', 'Chicken'),
    ('pork',    'Pork'),
    ('turkey',  'Turkey'),
    ('seafood', 'Seafood'),
    ('veg',     'Vegetarian'),
    ('eggs',    'Eggs / Breakfast'),
    ('other',   'Other'),
]

FAT_HEALTH_CHOICES = [
    ('F', 'Fatty'),
    ('H', 'Healthy'),
]

POPULARITY_CHOICES = [
    ('high',   'High'),
    ('medium', 'Medium'),
    ('low',    'Low'),
]


# Recipe ontology — Sean 2026-04-19 per project_recipe_authoring.md.
# Pre-selected based on content (all-raw → recipe, has sub_recipes → composed_dish,
# sub_recipes of composed → meal), but ALWAYS prompted, never silent.
LEVEL_CHOICES = [
    ('recipe',        'Recipe (ingredients only)'),
    ('composed_dish', 'Composed Dish (recipes + ingredients)'),
    ('meal',          'Meal (plate-level menu entry)'),
]


# "Big 15" dietary conflict vocabulary — Sean-approved 2026-04-19 per
# project_recipe_authoring.md. Applied as NEGATIVE descriptors: Recipe.conflicts
# lists what the recipe CONTAINS / conflicts with. Downstream (future Client
# model): match when set(client.avoid) & set(recipe.conflicts) == empty.
CONFLICT_CHOICES = [
    ('gluten',          'Gluten'),
    ('dairy',           'Dairy'),
    ('egg',             'Egg'),
    ('peanut',          'Peanut'),
    ('tree_nut',        'Tree nuts'),
    ('fish',            'Fish'),
    ('shellfish',       'Shellfish'),
    ('soy',             'Soy'),
    ('sesame',          'Sesame'),
    ('meat',            'Contains meat'),
    ('animal_products', 'Contains animal products'),
    ('high_sugar',      'High sugar'),
    ('high_sodium',     'High sodium'),
    ('not_kosher',      'Not kosher'),
    ('not_halal',       'Not halal'),
    ('other',           'Other'),
]
CONFLICT_LABELS = dict(CONFLICT_CHOICES)
# Short emoji icons for compact rendering on calendar cells
CONFLICT_ICONS = {
    'gluten': '🌾', 'dairy': '🥛', 'egg': '🥚', 'peanut': '🥜',
    'tree_nut': '🌰', 'fish': '🐟', 'shellfish': '🦐', 'soy': '🫘',
    'sesame': '·', 'meat': '🍖', 'animal_products': '🐄',
    'high_sugar': 'S+', 'high_sodium': 'Na+', 'not_kosher': 'K✗',
    'not_halal': 'H✗', 'other': '?',
}


class Recipe(models.Model):
    name           = models.CharField(max_length=200, unique=True)
    yield_servings = models.IntegerField(default=40)
    source_doc     = models.CharField(max_length=500, blank=True)
    notes          = models.TextField(blank=True)
    protein        = models.CharField(max_length=20, blank=True, choices=PROTEIN_CHOICES,
                                      help_text="Primary protein; used for menu arrangement rules.")
    fat_health     = models.CharField(max_length=1, blank=True, choices=FAT_HEALTH_CHOICES,
                                      help_text="Sean's (F)=Fatty / (H)=Healthy oscillation tag.")
    popularity     = models.CharField(max_length=10, blank=True, choices=POPULARITY_CHOICES,
                                      help_text="Popularity among residents (from Menu Guide highlight color).")
    conflicts      = models.JSONField(default=list, blank=True,
                                      help_text="Dietary conflicts this recipe CONTAINS (Big 15 vocab). "
                                                "Used for client-dietary-safety matching.")
    valid_slots    = models.JSONField(default=list, blank=True,
                                      help_text="Menu slots where this recipe belongs "
                                                "(cold_breakfast/hot_breakfast/lunch/dinner). "
                                                "Empty = appears in any slot.")

    # Learned popularity (auto-updated by signal on MealService save)
    learned_consumption_rate = models.DecimalField(
        max_digits=5, decimal_places=3, null=True, blank=True,
        help_text="Rolling avg total_consumption_rate from last ~10 services. "
                  "Null when fewer than 3 samples (fall back to 0.80 baseline).")
    learned_sample_count = models.IntegerField(
        default=0,
        help_text="Number of MealService samples backing learned_consumption_rate.")

    # Ontology + versioning (authoring flow)
    level          = models.CharField(max_length=20, choices=LEVEL_CHOICES, default='recipe',
                                      help_text="recipe = ingredients only; composed_dish = has sub_recipes; "
                                                "meal = plate-level menu entry.")
    parent_recipe  = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL,
                                       related_name='versions',
                                       help_text="If this is V2+, points to the prior version (V1).")
    version_number = models.IntegerField(default=1,
                                         help_text="1 for trunk, 2+ for later versions of the same lineage.")
    is_current     = models.BooleanField(default=True,
                                         help_text="Only one version per lineage is current. Menu links prefer current.")
    created_at     = models.DateTimeField(auto_now_add=True, null=True)

    def __str__(self):
        return self.name

    def conflict_labels(self):
        """Return list of (key, label, icon) tuples for rendering badges."""
        return [(k, CONFLICT_LABELS.get(k, k), CONFLICT_ICONS.get(k, '?'))
                for k in (self.conflicts or [])]

    def estimated_cost_breakdown(self):
        """Return dict: total Decimal, per_serving Decimal, priced count, total count, lines list."""
        from decimal import Decimal
        lines = []
        total = Decimal('0')
        priced = 0
        total_count = 0
        for ing in self.ingredients.all():
            if ing.sub_recipe:
                sub = ing.sub_recipe.estimated_cost_breakdown()
                # scale sub-recipe cost by quantity / sub yield_servings
                if ing.quantity and sub['total'] and ing.sub_recipe.yield_servings:
                    scaled = (sub['total'] * Decimal(ing.quantity)
                              / Decimal(ing.sub_recipe.yield_servings))
                    total += scaled
                    priced += 1
                    lines.append({'ingredient': ing, 'cost': scaled, 'note': 'sub-recipe'})
                else:
                    lines.append({'ingredient': ing, 'cost': None, 'note': 'sub-recipe incomplete'})
                total_count += 1
                continue
            cost, note = ing.estimated_cost()
            total_count += 1
            if cost is not None:
                total += cost
                priced += 1
            lines.append({'ingredient': ing, 'cost': cost, 'note': note})
        per_serving = (total / Decimal(self.yield_servings)) if self.yield_servings else None
        return {
            'total': total,
            'per_serving': per_serving,
            'priced': priced,
            'total_count': total_count,
            'coverage': (priced / total_count) if total_count else 0,
            'lines': lines,
        }

    def cost_for_headcount(self, headcount):
        """Scale the recipe's cost to a given headcount (useful for a menu serving N residents)."""
        from decimal import Decimal
        bd = self.estimated_cost_breakdown()
        if not bd['per_serving']:
            return None
        return (bd['per_serving'] * Decimal(headcount)).quantize(Decimal('0.01'))


class YieldReference(models.Model):
    """Canonical yield / conversion data — sourced from Book of Yields (8e) for internal use."""
    SECTION_CHOICES = [
        ('herbs_spices',  'Dry Herbs and Spices'),
        ('fresh_herbs',   'Fresh Herbs'),
        ('vegetables',    'Vegetables'),
        ('fruit',         'Fruit'),
        ('canned',        'Canned Foods'),
        ('dry_legumes',   'Dry Legumes'),
        ('grains',        'Rice/Grains/Cereals'),
        ('pasta',         'Pasta'),
        ('nuts_seeds',    'Nuts and Seeds'),
        ('flour',         'Flour/Meal/Bran/Crumbs'),
        ('sweeteners',    'Sweeteners'),
        ('baking',        'Special Baking Items'),
        ('fats_oils',     'Fats and Oils'),
        ('condiments',    'Condiments'),
        ('liquids',       'Liquids'),
        ('dairy',         'Dairy Products'),
        ('beverages',     'Coffee/Tea/Cocoa'),
        ('meats',         'Meats'),
        ('seafood',       'Seafood'),
        ('poultry',       'Poultry'),
    ]

    ingredient           = models.CharField(max_length=200)
    prep_state           = models.CharField(max_length=200, blank=True,
                                            help_text="e.g., 'peeled', 'grated', 'sliced about 1/4 in.'")
    section              = models.CharField(max_length=30, choices=SECTION_CHOICES, db_index=True)
    yield_pct            = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)

    ap_unit              = models.CharField(max_length=40, blank=True, help_text="As-purchased unit, e.g. 'pound', 'bunch'.")
    ap_weight_oz         = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    trimmed_unit         = models.CharField(max_length=40, blank=True)
    trimmed_weight_oz    = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    trimmed_count        = models.IntegerField(null=True, blank=True)
    measures_per_ap      = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True,
                                               help_text="e.g., cups per AP unit")
    ounce_weight_per_cup = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True)

    extras               = models.JSONField(null=True, blank=True,
                                            help_text="Section-specific fields (cooked yield, raw→cooked ratios, etc.)")

    source               = models.CharField(max_length=50, default='Book of Yields 8e')
    source_ref           = models.CharField(max_length=50, blank=True, help_text="e.g., 'p.50'")
    notes                = models.TextField(blank=True)
    product              = models.ForeignKey(Product, null=True, blank=True, on_delete=models.SET_NULL,
                                             related_name='yield_refs')

    class Meta:
        unique_together = [('ingredient', 'prep_state', 'section')]
        indexes = [models.Index(fields=['section', 'ingredient'])]

    def __str__(self):
        bits = [self.ingredient]
        if self.prep_state:
            bits.append(self.prep_state)
        return ", ".join(bits)


class RecipeIngredient(models.Model):
    recipe     = models.ForeignKey(Recipe, on_delete=models.CASCADE, related_name='ingredients')
    name_raw   = models.CharField(max_length=300)
    quantity   = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True,
                                     help_text="Edible-portion quantity the recipe calls for.")
    unit       = models.CharField(max_length=30, blank=True)
    yield_pct  = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        help_text="Edible yield % — only used if yield_ref is not set. "
                  "E.g., 81 for peeled carrots. Null = no adjustment.",
    )
    yield_ref  = models.ForeignKey(
        YieldReference, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='used_by_ingredients',
        help_text="If set, effective yield comes from this reference entry.",
    )
    product    = models.ForeignKey(Product, null=True, blank=True, on_delete=models.SET_NULL)
    sub_recipe = models.ForeignKey(
        Recipe, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='used_in', help_text="If set, this ingredient is another recipe (composed meal component).",
    )

    def __str__(self):
        return f"{self.quantity} {self.unit} {self.name_raw}".strip()

    @property
    def effective_yield_pct(self):
        if self.yield_ref and self.yield_ref.yield_pct is not None:
            return self.yield_ref.yield_pct
        return self.yield_pct

    @property
    def ap_quantity_needed(self):
        eff = self.effective_yield_pct
        if self.quantity is None or not eff:
            return self.quantity
        return self.quantity / (eff / 100)

    def estimated_cost(self):
        """Latest-price $ cost for this ingredient line. Returns (cost_or_None, note).

        When the absolute-latest ILI has no parseable case_size (common for
        Sysco known-code rows where the parser couldn't extract a pack size
        from the canonical description), walks backward up to 5 ILI rows
        looking for the first one that either has a parseable case_size OR
        has price_per_pound populated (both are sufficient for costing).
        This is freshness-preserving: we still use the most-recent usable
        invoice price, not the oldest one.
        """
        from decimal import Decimal
        from .cost_utils import ingredient_cost, case_size_candidates_for_cost, parse_case_size
        from .models import InvoiceLineItem   # self-import ok — called at runtime
        if not self.product or self.quantity is None:
            return None, 'missing product or quantity'

        # Walk ILI rows newest-first. Prefer the newest row that has EITHER
        # a parseable case_size OR a populated price_per_pound. If neither
        # is reachable within the top 5, fall back to the absolute newest.
        # B6: exclude math_flagged rows so anomaly-contaminated unit_price /
        # price_per_pound values don't poison recipe cost estimates. Per
        # Trust LAW. The walk continues to the NEXT-newest clean row.
        ili_candidates = list(InvoiceLineItem.objects
                              .filter(product=self.product)
                              .exclude(math_flagged=True)
                              .order_by('-invoice_date')[:5])
        if not ili_candidates:
            return None, 'no invoice history'

        latest = None
        fallback = ili_candidates[0]
        for ili in ili_candidates:
            # Product.default_case_size is the last-ditch fallback; try
            # parsing either the row's case_size OR the product default.
            has_case = bool(parse_case_size(ili.case_size or self.product.default_case_size or ''))
            has_ppp = ili.price_per_pound is not None
            if has_case or has_ppp:
                latest = ili
                break
        if latest is None:
            latest = fallback
        density = (self.yield_ref.ounce_weight_per_cup
                   if self.yield_ref and self.yield_ref.ounce_weight_per_cup
                   else None)

        candidates = case_size_candidates_for_cost(
            latest.case_size,
            latest.raw_description,
            product_default=self.product.default_case_size,
            product_name=self.product.canonical_name,
        )
        # Ensure at least one attempt even when no candidate parses
        attempt_cases = candidates or [latest.case_size or '']

        # Phase 3b: structured fields from latest ILI feed ingredient_cost.
        # count_per_lb_low/high enable per-piece cost for "2 strips bacon"
        # (Sean 2026-05-02). NULLs propagate cleanly — ingredient_cost
        # falls back to the existing dispatch when these are absent.
        latest_count_low = latest.count_per_lb_low
        latest_count_high = latest.count_per_lb_high

        def _try(qty, unit, yield_pct):
            last = (None, '')
            for cs in attempt_cases:
                cost, note = ingredient_cost(
                    qty, unit, self.name_raw,
                    latest.unit_price, cs,
                    yield_pct=yield_pct,
                    ounce_weight_per_cup=density,
                    price_per_pound=latest.price_per_pound,
                    count_per_lb_low=latest_count_low,
                    count_per_lb_high=latest_count_high,
                )
                if cost is not None:
                    return cost, note
                last = (cost, note)
            return last

        # Pass 1: normal dispatch. If the recipe unit is already
        # compatible with any candidate case_size (e.g. 'each' recipe vs
        # 'doz' case → count↔count), this succeeds and we stop.
        cost, note = _try(self.quantity, self.unit, self.effective_yield_pct)
        if cost is not None:
            return cost, note

        # Pass 2: piece-weight rewrite fallback. When recipe asks for a
        # size-word or each (medium/large/small/ea/each) AND yield_ref is
        # linked with a piece-type ap_unit (each/head/bunch) + ap_weight_oz
        # populated, rewrite (qty, unit) to AP weight in oz and retry.
        # Unlocks piece-unit RIs against weight/volume cases
        # (e.g. Carrot '4 each' vs '1/50LB' → 4 × 4.1oz = 16.4oz).
        #
        # Critical: ap_weight_oz IS already AP weight — must NOT pass
        # yield_pct in this branch. ingredient_cost's qty /= yield_pct/100
        # would over-scale into double-counted yield loss.
        _PIECE_RECIPE_UNITS = {'medium', 'large', 'small', 'ea', 'each'}
        _PIECE_AP_UNITS = {'each', 'head', 'bunch'}
        unit_lc = (self.unit or '').strip().lower()
        if (unit_lc in _PIECE_RECIPE_UNITS
                and self.yield_ref
                and self.yield_ref.ap_weight_oz
                and (self.yield_ref.ap_unit or '').strip().lower() in _PIECE_AP_UNITS):
            qty_in_oz = Decimal(self.quantity) * Decimal(self.yield_ref.ap_weight_oz)
            pw_cost, pw_note = _try(qty_in_oz, 'oz', None)
            if pw_cost is not None:
                return pw_cost, pw_note

        return cost, note


class Menu(models.Model):
    MEAL_SLOTS = [
        ('cold_breakfast', 'Cold Breakfast'),
        ('hot_breakfast', 'Hot Breakfast'),
        ('lunch', 'Lunch'),
        ('dinner', 'Dinner'),
    ]

    date                = models.DateField()
    meal_slot           = models.CharField(max_length=20, choices=MEAL_SLOTS)
    recipe              = models.ForeignKey(Recipe, null=True, blank=True, on_delete=models.SET_NULL)
    additional_recipes  = models.ManyToManyField(Recipe, blank=True, related_name='menus_as_side')
    dish_freetext       = models.CharField(max_length=200, blank=True)
    ingredients_raw     = models.TextField(blank=True)
    assignee            = models.CharField(max_length=20, choices=ASSIGNEE_CHOICES, blank=True)
    created_at          = models.DateTimeField(
        auto_now_add=True, null=True,
        help_text="Authoring timestamp — drives the recipe-coverage Flow metric "
                  "(% of newly-authored cells that got a recipe at authoring). "
                  "NULL on rows that predate instrumentation (2026-05-28).",
    )

    class Meta:
        unique_together = [('date', 'meal_slot')]
        indexes = [models.Index(fields=['date'])]

    def __str__(self):
        dish = self.recipe.name if self.recipe else self.dish_freetext
        return f"{self.date} {self.meal_slot}: {dish}"


class PrepTask(models.Model):
    date      = models.DateField()
    recipe    = models.ForeignKey(Recipe, on_delete=models.CASCADE, related_name='prep_tasks')
    assignee  = models.CharField(max_length=20, choices=ASSIGNEE_CHOICES, blank=True)
    completed = models.BooleanField(default=False)
    notes     = models.TextField(blank=True)

    class Meta:
        indexes = [models.Index(fields=['date'])]

    def __str__(self):
        return f"{self.date} prep: {self.recipe.name}"


class IngredientSkipNote(models.Model):
    """A breadcrumb left by Sean during bridge review so tomorrow's product-catalog
    pass knows which ingredient name was skipped and why.
    Stored per unique name_raw, not per RecipeIngredient.
    """
    name_raw   = models.CharField(max_length=200, unique=True)
    reason     = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name_raw}: {self.reason or '(no reason)'}"


class MenuFreetextComponent(models.Model):
    """A component on a Menu that isn't a linked Recipe yet — just a name + quantity.
    Promoted to a RecipeIngredient (name_raw + qty + unit) when the menu is saved as a meal.
    """
    menu     = models.ForeignKey('Menu', on_delete=models.CASCADE, related_name='freetext_components')
    name     = models.CharField(max_length=200)
    quantity = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True)
    unit     = models.CharField(max_length=30, blank=True)
    position = models.IntegerField(default=0)

    class Meta:
        ordering = ['position', 'id']

    def __str__(self):
        q = f"{self.quantity} {self.unit}".strip() if self.quantity else ''
        return f"{self.name} {q}".strip()


class Census(models.Model):
    date      = models.DateField(unique=True)
    headcount = models.IntegerField()
    notes     = models.CharField(max_length=200, blank=True)

    class Meta:
        verbose_name_plural = 'Census'

    def __str__(self):
        return f"{self.date}: {self.headcount}"


class MealService(models.Model):
    """Per-menu-slot service record: how much was prepped, how much leftover,
    how much discarded (and when). Foundation for the popularity-learning loop
    per `project_popularity_learning.md`.

    Two-touchpoint capture (not yet UI'd — schema only):
      - **Cleanup (post-service):** fill prepped_qty + post_service_leftover_qty
      - **Disposal (at shelf-life end, rare):** fill discarded_qty + discarded_at

    Derived signals (application-layer, not stored):
      - immediate_eat_rate = (prepped - leftover) / prepped
      - redemption_rate = (leftover - discarded) / leftover  (when leftover > 0)
      - total_consumption_rate = (prepped - discarded) / prepped  ← the master signal

    Unit is per-dish and carried on this row (pans, portions, lbs, etc.) — the
    memory's design decision is to not try to normalize across dishes.
    """
    menu = models.ForeignKey(Menu, on_delete=models.CASCADE, related_name='service_records')
    prepped_qty = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True,
                                      help_text="Amount prepped for service, in the dish's native unit.")
    post_service_leftover_qty = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True,
                                                    help_text="Amount leftover after service; goes to client fridge.")
    discarded_qty = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True,
                                        help_text="Amount discarded at shelf-life end. Usually 0.")
    discarded_at = models.DateTimeField(null=True, blank=True,
                                        help_text="When disposal happened (typically day 5 of leftover shelf life).")
    unit = models.CharField(max_length=30, blank=True,
                            help_text="Per-dish convention: pans, portions, lbs, etc.")
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['menu']),
            models.Index(fields=['discarded_at']),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f"MealService {self.menu.date} {self.menu.meal_slot}: prepped={self.prepped_qty} leftover={self.post_service_leftover_qty}"

    @property
    def immediate_eat_rate(self):
        """Fraction eaten at service (before leftovers). None if inputs missing."""
        if not self.prepped_qty or self.post_service_leftover_qty is None:
            return None
        from decimal import Decimal
        if self.prepped_qty == 0:
            return None
        return ((self.prepped_qty - self.post_service_leftover_qty)
                / self.prepped_qty).quantize(Decimal('0.001'))

    @property
    def total_consumption_rate(self):
        """Master popularity signal: (prepped - discarded) / prepped.
        If discarded is null, treat as 0 (no disposal recorded = nothing thrown out)."""
        if not self.prepped_qty:
            return None
        from decimal import Decimal
        discarded = self.discarded_qty or Decimal('0')
        if self.prepped_qty == 0:
            return None
        return ((self.prepped_qty - discarded) / self.prepped_qty).quantize(Decimal('0.001'))


class StandardPortionReference(models.Model):
    """Canonical per-portion sizes from Book of Yields 8e Chapter 15.

    Values stored as strings to preserve source fidelity ('1/4th whole 3 lb
    fryer', '3 strips', '5 fl. oz.' etc.). App-layer parses when needed.
    """
    CATEGORY_CHOICES = [
        ('soup_salad_bread', 'Soup, Salad, Bread'),
        ('beef_entrees', 'Beef Entrees'),
        ('chicken_entrees', 'Chicken Entrees'),
        ('seafood_entrees', 'Seafood Entrees'),
        ('pork_entrees', 'Pork Entrees'),
        ('veal', 'Veal'),
        ('pasta_entree', 'Pasta Entree'),
        ('potatoes', 'Potatoes'),
        ('desserts', 'Desserts'),
        ('beverages', 'Beverages'),
        ('breakfast_items', 'Breakfast Items'),
        ('lunch_items', 'Lunch Items'),
        ('hors_doeuvre', "Hors d'Oeuvre"),
    ]

    menu_item        = models.CharField(max_length=120)
    category         = models.CharField(max_length=32, choices=CATEGORY_CHOICES)
    average_measure  = models.CharField(max_length=40, blank=True)
    low_range        = models.CharField(max_length=40, blank=True)
    high_range       = models.CharField(max_length=40, blank=True)
    source           = models.CharField(max_length=60, default='Book of Yields 8e Ch 15')
    source_ref       = models.CharField(max_length=20, blank=True)

    class Meta:
        ordering = ['category', 'menu_item']
        indexes = [models.Index(fields=['category', 'menu_item'])]
        constraints = [
            models.UniqueConstraint(fields=['category', 'menu_item'], name='uniq_portion_by_category_item'),
        ]

    def __str__(self):
        return f"{self.menu_item} ({self.average_measure})"


# Canonical drift rejections retired 2026-05-02 — unified into
# ProductMappingProposal (status='rejected' + source='drift_audit').
# The previous CanonicalDriftRejection model was load-bearing for ~30
# minutes between phase 1 of the unification and this retirement.


class VendorPriceList(models.Model):
    """Vendor-quoted list price for a SKU at a given order unit.

    Sourced from vendor-distributed order guides (e.g. Farm Art's default
    order guide CSV). Distinct from `ProductMapping` — this captures
    PRICE per unit, not raw_description → canonical Product mapping.
    Same SKU can appear at multiple units (CASE / HALF_CASE / EACH / LB)
    with different list prices, exposing the vendor's sub-case premium.

    Companion to ProductMapping for cost-calc + price-audit:
        ProductMapping → which Product is this SKU?
        VendorPriceList → what does this SKU cost per unit?

    `ach_discount_pct` captures vendor-specific payment-method discount
    (Farm Art applies 1% for ACH); `ach_price` is computed at read time.
    `captured_at` lets future re-ingests track price drift over time
    (current model overwrites on (vendor, sku, unit) — promote to
    historical-mode by adding captured_at to unique_together).
    """
    vendor          = models.ForeignKey(Vendor, on_delete=models.CASCADE,
                                        related_name='price_list_entries')
    sku             = models.CharField(max_length=40, db_index=True,
                                       help_text="Vendor's SKU code (e.g. 'EGS', 'ID12')")
    raw_description = models.CharField(max_length=500,
                                       help_text="Vendor's display name; matches "
                                                 "ProductMapping.description on invoices")
    unit            = models.CharField(max_length=30,
                                       help_text="Order unit: CASE / HALF_CASE / EACH / "
                                                 "LB / BAG / BOX / FLAT / BUSHEL / KILOGRAM "
                                                 "/ GALLON / CLAMSHELL_BOX / UNIT / etc. "
                                                 "Vendor-specific vocabulary.")
    list_price      = models.DecimalField(max_digits=8, decimal_places=2)
    ach_discount_pct = models.DecimalField(max_digits=5, decimal_places=4,
                                           default=0,
                                           help_text="Decimal e.g. 0.01 = 1% ACH discount.")
    captured_at     = models.DateField(help_text="Date the source order guide was captured.")
    source_file     = models.CharField(max_length=200, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['vendor', 'sku', 'unit'],
                                    name='uniq_vendor_sku_unit'),
        ]
        indexes = [models.Index(fields=['vendor', 'sku'])]
        ordering = ['vendor', 'sku', 'unit']

    @property
    def ach_price(self):
        """List price × (1 - ach_discount_pct). The price Sean pays per unit."""
        return self.list_price * (1 - self.ach_discount_pct)

    def __str__(self):
        return f"{self.vendor.name}/{self.sku}/{self.unit} ${self.list_price}"


class InvoiceLineEdit(models.Model):
    """Audit row capturing every Sean-initiated edit to an InvoiceLineItem.

    Each save through `/invoices/<id>/lines/<id>/edit/` writes one of these
    so the edit history is replayable + auditable. Per Trust LAW: when
    Sean overrides parser output, the system records WHAT changed, WHEN,
    WHO did it, and WHY (reason + note). Downstream consumers know that
    user-edited rows are higher-trust than auto-extracted rows.

    Pairs with `InvoiceLineItem.user_edited` (boolean — set True on
    first edit) and `InvoiceValidationStatus.verified_by/verified_at`
    (set when Sean explicitly approves the whole invoice).

    Phase 1.2 of L1 Invoice Reconciliation. See project_quickbooks_roadmap.md.
    """
    REASON_CHOICES = [
        ('', '— none —'),
        ('manual_correction', 'Manual correction (parser error / OCR garble)'),
        ('damaged_transit', 'Damaged in transit (driver credit on paper)'),
        ('short_ship', 'Short ship (delivered less than billed)'),
        ('quality_issue', 'Quality issue / return'),
        ('pricing_correction', 'Pricing correction'),
        ('handwritten_addition', 'Hand-written invoice / line addition'),
        ('handwritten_credit', 'Hand-written credit (driver wrote on paper)'),
        ('portal_credit_match', 'Reconciled to vendor portal credit'),
        ('other', 'Other (see note)'),
    ]
    ili = models.ForeignKey(
        'InvoiceLineItem', on_delete=models.CASCADE,
        related_name='edit_history',
    )
    edited_by = models.ForeignKey(
        'auth.User', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='+',
    )
    edited_at = models.DateTimeField(auto_now_add=True, db_index=True)
    before = models.JSONField(
        default=dict,
        help_text='Snapshot of editable ILI fields BEFORE this edit. '
                  'Keys: quantity, unit_price, extended_amount, case_size, '
                  'raw_description.',
    )
    after = models.JSONField(
        default=dict,
        help_text='Snapshot of editable ILI fields AFTER this edit.',
    )
    reason = models.CharField(
        max_length=30, choices=REASON_CHOICES, blank=True,
        help_text='Categorical reason. Drives downstream filtering '
                  '(e.g. credits feed L2 InvoiceCredit reconciliation).',
    )
    note = models.TextField(
        blank=True,
        help_text='Free-text context for the edit. Why, what was on '
                  'paper vs what parser captured, etc.',
    )
    cleared_math_flag = models.BooleanField(
        default=False,
        help_text='True if this edit caused math_flagged to flip from '
                  'True → False (math now reconciles).',
    )

    class Meta:
        ordering = ['-edited_at']
        indexes = [
            models.Index(fields=['ili', '-edited_at']),
        ]

    def __str__(self):
        return f"Edit on ILI #{self.ili_id} at {self.edited_at} by {self.edited_by}"


class InvoiceValidationStatus(models.Model):
    """Per-invoice validation status. One row per (vendor, invoice_number).

    First invoice-level (not line-level) record in the DB. Records:
      - Items extracted vs printed invoice_total
      - Per-section reconciliation outcome (parser_sum vs printed GROUP TOTAL)
      - Overall PASS/REVIEW/FAIL classification

    Populated by `validate_all_invoices` mgmt command and by `db_write`
    after every ingestion. Drives the validation-failure surface (B5
    durable layer) per `project_parser_accuracy_goal.md`. Per Trust LAW,
    REVIEW/FAIL invoices need Sean's eye before downstream consumers
    (Synergy sheet, recipe cost calc) trust the underlying ILI rows.
    """

    STATUS_CHOICES = [
        # Invoice math reconciles within tolerance — every consumer can trust
        # the underlying ILI rows.
        ('pass',    'PASS — all sections + invoice_total reconcile within tolerance'),
        # Some sections diverge but invoice_total is approximately right —
        # extraction has gaps but isn't catastrophically wrong.
        ('review',  'REVIEW — section-level diffs exceed tolerance'),
        # Items_sum diverges from invoice_total by >10% — the system can't
        # vouch for these numbers; downstream consumers should not use them.
        ('fail',    'FAIL — items_sum vs invoice_total >10% off'),
        # Section data unavailable (no GROUP TOTAL extracted, or partial
        # cache) — can't reconcile but no obvious failure.
        ('partial', 'PARTIAL — invoice_total or section data missing'),
    ]

    vendor          = models.ForeignKey(Vendor, on_delete=models.CASCADE)
    invoice_number  = models.CharField(max_length=20, db_index=True)
    invoice_date    = models.DateField(null=True, blank=True)

    items_count     = models.IntegerField(default=0)
    items_sum       = models.DecimalField(max_digits=10, decimal_places=2,
                                           null=True, blank=True)
    invoice_total   = models.DecimalField(max_digits=10, decimal_places=2,
                                           null=True, blank=True,
                                           help_text="Printed INVOICE TOTAL extracted from the invoice.")
    invoice_gap     = models.DecimalField(max_digits=10, decimal_places=2,
                                           null=True, blank=True,
                                           help_text="items_sum − invoice_total. Negative = items missing.")
    invoice_gap_pct = models.DecimalField(max_digits=6, decimal_places=2,
                                           null=True, blank=True,
                                           help_text="abs(invoice_gap) / invoice_total × 100.")

    # L1 Phase 2 (2026-05-18): credits expected from the vendor.
    # Sum of (audit_edit.before.extended_amount − audit_edit.after.extended_amount)
    # across InvoiceLineEdit rows whose reason flags a pending credit
    # (damaged_transit, short_ship, quality_issue, handwritten_credit).
    # When items_sum + credits_pending ≈ invoice_total, the invoice
    # is fully explained — the gap represents money the vendor owes us,
    # not a parser bug. Status classifier treats this as PASS.
    # Pairs with L2 InvoiceCredit table (roadmapped) for credit-memo
    # reconciliation against QuickBooks ledger.
    credits_pending = models.DecimalField(
        max_digits=10, decimal_places=2,
        null=True, blank=True,
        help_text="$ of audit edits with credit-pending reason "
                  "(damaged_transit/short_ship/quality_issue/handwritten_credit). "
                  "Expected credit-memo amount from vendor.",
    )

    sections_total      = models.IntegerField(default=0,
                                              help_text="Number of distinct sections detected.")
    sections_reconciled = models.IntegerField(default=0,
                                              help_text="Sections where parser_sum matches printed GROUP TOTAL within $0.50.")
    sections_with_gap   = models.IntegerField(default=0,
                                              help_text="Sections with diff > $0.50.")

    # Full per-section reconciliation list — same shape as
    # section_validator.reconcile() output. Stored for audit + UI display.
    section_reconciliation = models.JSONField(
        default=list, blank=True,
        help_text="List of {section, parser_sum, printed_total, diff_abs, diff_pct, item_count}.",
    )

    # Cache hashes (sha256 prefixes) that contributed to this invoice's
    # validation. Lets us trace back to the OCR sources that fed parsing.
    cache_hashes    = models.JSONField(
        default=list, blank=True,
        help_text="OCR cache file SHA256 hashes that fed this validation.",
    )

    status          = models.CharField(max_length=10, choices=STATUS_CHOICES,
                                        default='partial', db_index=True)
    notes           = models.TextField(blank=True)
    last_validated  = models.DateTimeField(auto_now=True)

    # L1 Phase 1.2: Sean-explicit approval gate distinct from auto-validation.
    # `status='pass'` means "math reconciled automatically"; `verified_by`
    # means "Sean reviewed it and signed off." Downstream consumers can
    # opt to require user-verification (eventual L7 QB push criterion).
    verified_by     = models.ForeignKey(
        'auth.User', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='+',
        help_text='User who explicitly verified this invoice. NULL means '
                  'auto-validation only.',
    )
    verified_at     = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [('vendor', 'invoice_number')]
        indexes = [
            models.Index(fields=['status', '-last_validated']),
            models.Index(fields=['vendor', '-invoice_date']),
        ]
        ordering = ['-invoice_date', 'vendor', 'invoice_number']

    def __str__(self):
        return (f"{self.vendor.name} {self.invoice_number} "
                f"[{self.status}] {self.invoice_gap_pct or 0}%")

    def revalidate_from_ili(self):
        """Recompute items_sum / gap / section parser_sums / status from
        the current state of related InvoiceLineItem rows. Saves the row.

        Called after user edits/adds (invoice_line_edit / invoice_line_add)
        so the L1 review surface stays fresh without waiting for the next
        `validate_all_invoices --apply` cron run. The IVS stale-after-edit
        gap was the audit-flow blocker — Sean fixes milk qty 1→2 via UI,
        DAIRY section reconciliation should close immediately, not at
        cron time.

        Distinct from validate_all_invoices in TWO ways:
          1. Does NOT re-parse OCR caches — trusts current ILI table state
             (user edits are authoritative; parser output is stale relative
             to them).
          2. Preserves printed_total values in section_reconciliation
             (those came from OCR-side reconciliation; user edits don't
             change the printed invoice). Only parser_sum / diff_abs /
             diff_pct fields are recomputed from current ILI.

        Filter strategy: prefer invoice_number when populated (Phase 4c+
        ILI rows), fall back to (vendor, invoice_date) for older rows
        that pre-date the invoice_number backfill.
        """
        from decimal import Decimal
        from collections import defaultdict

        # Prefer invoice_number when populated on both sides. Fall back to
        # (vendor, invoice_date) when the invoice_number filter returns
        # nothing — historical ILI rows that pre-date the 2026-05-14
        # backfill_missing_invoice_numbers run may have IVS.invoice_number
        # set but ILI.invoice_number=''. Matches invoice_detail view's
        # (vendor, invoice_date) join shape.
        ilis = []
        if self.invoice_number:
            ilis = list(InvoiceLineItem.objects.filter(
                vendor=self.vendor,
                invoice_number=self.invoice_number,
            ))
        if not ilis:
            ilis = list(InvoiceLineItem.objects.filter(
                vendor=self.vendor,
                invoice_date=self.invoice_date,
            ))

        items_count = len(ilis)
        items_sum = sum((ili.extended_amount or Decimal('0')) for ili in ilis)

        # Recompute parser_sum per section from current ILI rows. Preserve
        # printed_total values from the existing section_reconciliation
        # JSON (those reflect OCR-side reconciliation that user edits
        # don't invalidate).
        sections_with_gap = 0
        sections_reconciled = 0
        if self.section_reconciliation:
            new_parser_by_section: dict = defaultdict(lambda: Decimal('0'))
            for ili in ilis:
                sec = ili.section_hint or ''
                new_parser_by_section[sec] += (ili.extended_amount or Decimal('0'))

            updated_recon = []
            for r in self.section_reconciliation:
                sec_name = r.get('section', '')
                new_parser_sum = float(new_parser_by_section.get(sec_name, 0))
                printed = r.get('printed_total')
                new_diff_abs = (round(new_parser_sum - printed, 2)
                                if printed is not None else None)
                new_diff_pct = (round(new_diff_abs / printed * 100, 2)
                                if printed and printed != 0
                                and new_diff_abs is not None
                                else None)
                # Update item_count for this section too (user adds can
                # change the count distribution).
                sec_count = sum(1 for ili in ilis if (ili.section_hint or '') == sec_name)
                updated_recon.append({
                    **r,
                    'parser_sum': round(new_parser_sum, 2),
                    'diff_abs': new_diff_abs,
                    'diff_pct': new_diff_pct,
                    'item_count': sec_count,
                })
            self.section_reconciliation = updated_recon
            sections_with_gap = sum(
                1 for r in updated_recon
                if r.get('diff_abs') is not None
                and abs(float(r['diff_abs'])) >= 0.50
            )
            sections_reconciled = len(updated_recon) - sections_with_gap

        # L1 Phase 2 (2026-05-18): compute credits_pending — the $
        # the vendor owes back due to audit reductions with credit-
        # pending reasons. When items_sum + credits_pending ≈
        # invoice_total, the invoice fully reconciles (the gap is
        # expected-credit, not parser error).
        CREDIT_REASONS = ('damaged_transit', 'short_ship',
                          'quality_issue', 'handwritten_credit')
        credits_pending = Decimal('0')
        ili_ids = [i.id for i in ilis]
        if ili_ids:
            from myapp.models import InvoiceLineEdit
            for edit in InvoiceLineEdit.objects.filter(
                    ili_id__in=ili_ids, reason__in=CREDIT_REASONS):
                try:
                    before_ext = Decimal(str(edit.before.get(
                        'extended_amount', '0')))
                    after_ext = Decimal(str(edit.after.get(
                        'extended_amount', '0')))
                    delta = before_ext - after_ext
                    if delta > 0:
                        credits_pending += delta
                except (TypeError, ValueError, KeyError):
                    pass

        self.items_sum = items_sum
        self.items_count = items_count
        self.sections_with_gap = sections_with_gap
        self.sections_reconciled = sections_reconciled
        self.credits_pending = credits_pending if credits_pending > 0 else None

        if self.invoice_total:
            self.invoice_gap = items_sum - self.invoice_total
            if self.invoice_total != 0:
                self.invoice_gap_pct = (
                    abs(self.invoice_gap) / self.invoice_total * Decimal('100')
                ).quantize(Decimal('0.01'))
            else:
                self.invoice_gap_pct = None

        # Re-classify through the same path validate_all_invoices uses.
        # Import locally to avoid circular import at module load.
        # L1 Phase 2: pass credits_pending so classifier treats fully-
        # explained gaps as PASS (gap matches expected vendor credit).
        from myapp.management.commands.validate_all_invoices import _classify
        self.status = _classify(
            float(items_sum),
            float(self.invoice_total) if self.invoice_total else None,
            self.section_reconciliation or [],
            credits_pending=float(credits_pending),
        )
        self.save()
