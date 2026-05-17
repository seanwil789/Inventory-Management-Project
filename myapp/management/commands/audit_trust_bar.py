"""Trust-bar audit (Tier A of trust-bar measurement plan).

Reads InvoiceLineEdit + IVS.verified_by data already in the DB and computes
per-vendor per-field parser error rates from the corpus of audited invoices.

Distinct from the existing audit_* commands: every other audit measures
INTERNAL consistency (math reconciliation, cross-path agreement, mapping
identity). This is the only one that measures EXTERNAL truth — parser
output vs the paper-truth value Sean encoded by editing the line through
the L1 reconciliation UI.

Algorithm:
  1. Identify audited invoices — those with at least one InvoiceLineEdit
     row OR with IVS.verified_by set.
  2. Denominator = total ILI rows currently on each audited invoice.
     Caveat: doesn't count lines parser captured that Sean later deleted.
  3. Walk each edited ILI's edit chain (first edit's `before` vs last
     edit's `after`) and count per-field changes as parser errors.
  4. ADD-line edits (before={}) count as recall misses, not field errors.
  5. Wilson 95% CI on per-field error rate.

Read-only. No DB writes.

Caveat: requires user_edited lines AND IVS.verified_by signals to be
trustworthy. Without verified_by, unedited lines on audited invoices are
ASSUMED correct (could just be lines you didn't get to yet). The audit
flags this verification debt.

Usage:
    python manage.py audit_trust_bar
    python manage.py audit_trust_bar --days 30
"""
import json
import math
from collections import defaultdict
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from myapp.models import (
    InvoiceLineEdit, InvoiceLineItem, InvoiceValidationStatus,
)


FIELDS = ('quantity', 'unit_price', 'extended_amount', 'case_size')


def _wilson_ci(successes, n, z=1.96):
    """95% Wilson confidence interval for a binomial proportion.
    Returns (low, high) tuple in [0,1], or (None, None) when n==0."""
    if n == 0:
        return (None, None)
    p = successes / n
    denom = 1 + z**2 / n
    centre = (p + z**2 / (2*n)) / denom
    spread = z * math.sqrt(p*(1-p)/n + z**2/(4*n**2)) / denom
    return (max(0.0, centre - spread), min(1.0, centre + spread))


def _norm_field(v):
    """Normalize a before/after field value for comparison. Handles
    str/decimal/None variants the JSONField may carry."""
    if v is None:
        return ''
    return str(v).strip()


class Command(BaseCommand):
    help = ("Per-vendor per-field parser error rate from "
            "InvoiceLineEdit + IVS.verified_by audit data.")

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=0,
                            help='Window: only edits within last N days (0 = all-time)')
        parser.add_argument('--report-json',
                            default='.claude/trust_bar_report.json')

    def handle(self, *args, days=0, report_json='', **kw):
        # ── Pull audit data ───────────────────────────────────────────
        edits_qs = (InvoiceLineEdit.objects
                    .select_related('ili', 'ili__vendor'))
        if days > 0:
            cutoff = timezone.now() - timedelta(days=days)
            edits_qs = edits_qs.filter(edited_at__gte=cutoff)
        edits = list(edits_qs.order_by('edited_at'))

        verified_ivs = list(InvoiceValidationStatus.objects
                            .exclude(verified_by__isnull=True)
                            .select_related('vendor'))

        # ── Group edits by ILI (handle multi-edit chains) ─────────────
        edits_by_ili: dict = defaultdict(list)
        for e in edits:
            edits_by_ili[e.ili_id].append(e)

        # ── Identify audited invoices: (vendor_id, invoice_number) ────
        # An invoice is "audited" if any of its lines has been edited OR
        # if its IVS has verified_by set.
        audited_keys: set = set()
        for ili_id in edits_by_ili:
            ili = edits_by_ili[ili_id][0].ili
            if ili.invoice_number and ili.vendor_id:
                audited_keys.add((ili.vendor_id, ili.invoice_number))
        for ivs in verified_ivs:
            if ivs.invoice_number:
                audited_keys.add((ivs.vendor_id, ivs.invoice_number))

        # ── Per-vendor aggregation ────────────────────────────────────
        per_vendor: dict = defaultdict(lambda: {
            'invoices_audited': set(),       # set of invoice_numbers
            'invoices_verified': set(),
            'total_lines_reviewed': 0,
            'lines_edited': 0,
            'lines_added': 0,                # parser-recall misses
            'field_errors': {f: 0 for f in FIELDS},
            'edits_by_reason': defaultdict(int),
            'cleared_math_flag': 0,
            'sample_edits': [],
        })

        # Denominator: total ILI rows currently on each audited invoice
        for (vendor_id, invoice_number) in audited_keys:
            ilis = InvoiceLineItem.objects.filter(
                vendor_id=vendor_id, invoice_number=invoice_number
            ).select_related('vendor')
            first = ilis.first()
            if not first:
                continue
            v_name = first.vendor.name if first.vendor else f'vendor#{vendor_id}'
            per_vendor[v_name]['invoices_audited'].add(invoice_number)
            per_vendor[v_name]['total_lines_reviewed'] += ilis.count()

        # Verified-by counts
        for ivs in verified_ivs:
            v_name = ivs.vendor.name if ivs.vendor else 'Unknown'
            per_vendor[v_name]['invoices_verified'].add(ivs.invoice_number)

        # ── Walk each edited ILI's edit chain ─────────────────────────
        for ili_id, ili_edits in edits_by_ili.items():
            ili_edits.sort(key=lambda e: e.edited_at)
            first_before = ili_edits[0].before or {}
            last_after  = ili_edits[-1].after  or {}

            ili = ili_edits[0].ili
            v_name = ili.vendor.name if ili.vendor else 'Unknown'

            # Reason + math-flag tallies per edit (not per ILI)
            for e in ili_edits:
                per_vendor[v_name]['edits_by_reason'][e.reason or '(none)'] += 1
                if e.cleared_math_flag:
                    per_vendor[v_name]['cleared_math_flag'] += 1

            # ADD-line: before is empty dict
            if not first_before:
                per_vendor[v_name]['lines_added'] += 1
                if len(per_vendor[v_name]['sample_edits']) < 5:
                    per_vendor[v_name]['sample_edits'].append({
                        'kind': 'add',
                        'ili_id': ili_id,
                        'invoice_number': ili.invoice_number,
                        'after': last_after,
                        'reason': ili_edits[0].reason,
                    })
                continue

            per_vendor[v_name]['lines_edited'] += 1
            field_changes = []
            for field in FIELDS:
                b = _norm_field(first_before.get(field))
                a = _norm_field(last_after.get(field))
                if b != a:
                    per_vendor[v_name]['field_errors'][field] += 1
                    field_changes.append(f"{field}: {b!r} → {a!r}")
            if field_changes and len(per_vendor[v_name]['sample_edits']) < 5:
                per_vendor[v_name]['sample_edits'].append({
                    'kind': 'edit',
                    'ili_id': ili_id,
                    'invoice_number': ili.invoice_number,
                    'changes': field_changes,
                    'reason': ili_edits[0].reason,
                })

        # ── Compose report ────────────────────────────────────────────
        report = {
            'window_days': days,
            'total_edits_in_window': len(edits),
            'total_audited_invoices': sum(len(v['invoices_audited']) for v in per_vendor.values()),
            'total_verified_invoices': sum(len(v['invoices_verified']) for v in per_vendor.values()),
            'by_vendor': {},
        }

        for v, b in sorted(per_vendor.items()):
            n_lines = b['total_lines_reviewed']
            field_rates = {}
            for f in FIELDS:
                err = b['field_errors'][f]
                low, high = _wilson_ci(err, n_lines)
                field_rates[f] = {
                    'errors': err,
                    'rate': (err / n_lines) if n_lines else None,
                    'ci_low': low,
                    'ci_high': high,
                }
            # Added-line (recall) rate uses the same denominator
            add_low, add_high = _wilson_ci(b['lines_added'], n_lines)
            verification_debt = (len(b['invoices_audited'])
                                  - len(b['invoices_verified']))

            report['by_vendor'][v] = {
                'invoices_audited': len(b['invoices_audited']),
                'invoices_verified': len(b['invoices_verified']),
                'verification_debt': verification_debt,
                'total_lines_reviewed': n_lines,
                'lines_edited': b['lines_edited'],
                'lines_added': b['lines_added'],
                'recall_miss_rate': {
                    'count': b['lines_added'],
                    'rate': (b['lines_added'] / n_lines) if n_lines else None,
                    'ci_low': add_low,
                    'ci_high': add_high,
                },
                'field_errors': field_rates,
                'edits_by_reason': dict(b['edits_by_reason']),
                'cleared_math_flag_count': b['cleared_math_flag'],
                'sample_edits': b['sample_edits'],
            }

        out = settings.BASE_DIR / report_json
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, 'w') as f:
            json.dump(report, f, indent=2, default=str)

        # ── Pretty stdout ─────────────────────────────────────────────
        self.stdout.write("")
        self.stdout.write(f"Trust-Bar Audit"
                          + (f" — last {days} days" if days else " — all time"))
        self.stdout.write(f"Total edits: {report['total_edits_in_window']} "
                          f"on {report['total_audited_invoices']} invoices "
                          f"({report['total_verified_invoices']} fully verified)")
        self.stdout.write("")

        if not per_vendor:
            self.stdout.write("No edits found in window. "
                              "(Trust-bar measurement requires audit-edits "
                              "through /invoices/<id>/lines/<id>/edit/ — Pi has the data.)")
            self.stdout.write(f"\nReport written to {out}")
            return

        # Per-vendor table
        hdr = (f"{'Vendor':<32}{'Aud':>5}{'Ver':>5}{'Lines':>7}"
               f"{'Qty err':>10}{'UPx err':>10}{'CS err':>10}{'Ext err':>10}"
               f"{'Recall miss':>13}")
        self.stdout.write(hdr)
        self.stdout.write('-' * len(hdr))

        def _fmt(field_data):
            if field_data['rate'] is None:
                return '   —      '
            r = field_data['rate']
            lo = field_data['ci_low']
            hi = field_data['ci_high']
            return f"{r:.1%} ±{(hi-lo)/2:.0%}"

        for v, info in sorted(report['by_vendor'].items()):
            fe = info['field_errors']
            rm = info['recall_miss_rate']
            self.stdout.write(
                f"{v[:32]:<32}"
                f"{info['invoices_audited']:>5}"
                f"{info['invoices_verified']:>5}"
                f"{info['total_lines_reviewed']:>7}"
                f"{_fmt(fe['quantity']):>10}"
                f"{_fmt(fe['unit_price']):>10}"
                f"{_fmt(fe['case_size']):>10}"
                f"{_fmt(fe['extended_amount']):>10}"
                f"{_fmt(rm):>13}"
            )

        self.stdout.write("")
        self.stdout.write("Legend: Aud=invoices with edits or verified-by; Ver=invoices with verified_by set")
        self.stdout.write("        Rates show error% ± half-CI95-width. CIs are Wilson intervals.")
        self.stdout.write("        Recall miss = parser dropped the line (Sean had to ADD it)")
        self.stdout.write("")

        # Verification debt summary
        total_debt = sum(info['verification_debt']
                         for info in report['by_vendor'].values())
        if total_debt > 0:
            self.stdout.write(f"⚠ Verification debt: {total_debt} invoices have edits but no verified_by signal.")
            self.stdout.write("  Numbers above assume unedited lines on those invoices are correct — ")
            self.stdout.write("  click Verify on each fully-audited invoice to make this assumption explicit.")
            self.stdout.write("")

        # Edit-reason distribution (top 10 across all vendors)
        reason_counts: dict = defaultdict(int)
        for v_info in report['by_vendor'].values():
            for r, n in v_info['edits_by_reason'].items():
                reason_counts[r] += n
        if reason_counts:
            self.stdout.write("Edit reasons:")
            for r, n in sorted(reason_counts.items(), key=lambda kv: -kv[1])[:10]:
                self.stdout.write(f"  {r:<30} {n}")
            self.stdout.write("")

        self.stdout.write(f"Report written to {out}")
