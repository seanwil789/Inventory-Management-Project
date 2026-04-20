"""
Rename match_confidence values from hyphen to underscore naming, and add
keyword_batch / manual_review as first-class choices (they were being written
to the DB but weren't in the model's choices list).

Pre-existing DB values (pre-2026-04-19):
  vendor-exact  → vendor_exact
  vendor-fuzzy  → vendor_fuzzy
  stripped-fuzzy → stripped_fuzzy
  keyword-batch → keyword_batch   (was not in choices)
  manual-review → manual_review   (was not in choices)
  exact, fuzzy, code, unmatched, blank — unchanged
  global_exact / global_fuzzy — never written, removed from choices
"""
from django.db import migrations, models


_HYPHEN_TO_UNDERSCORE = {
    'vendor-exact':   'vendor_exact',
    'vendor-fuzzy':   'vendor_fuzzy',
    'stripped-fuzzy': 'stripped_fuzzy',
    'keyword-batch':  'keyword_batch',
    'manual-review':  'manual_review',
}


def forward(apps, schema_editor):
    InvoiceLineItem = apps.get_model('myapp', 'InvoiceLineItem')
    for old, new in _HYPHEN_TO_UNDERSCORE.items():
        n = InvoiceLineItem.objects.filter(match_confidence=old).update(match_confidence=new)
        if n:
            print(f'    {old!r:20s} → {new!r:20s}  ({n} rows)')


def reverse(apps, schema_editor):
    InvoiceLineItem = apps.get_model('myapp', 'InvoiceLineItem')
    for new, old in {v: k for k, v in _HYPHEN_TO_UNDERSCORE.items()}.items():
        InvoiceLineItem.objects.filter(match_confidence=new).update(match_confidence=old)


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0013_standardportionreference'),
    ]

    operations = [
        migrations.RunPython(forward, reverse),
        migrations.AlterField(
            model_name='invoicelineitem',
            name='match_confidence',
            field=models.CharField(
                blank=True,
                max_length=20,
                choices=[
                    ('code', 'SUPC Code Match'),
                    ('exact', 'Exact Description Match'),
                    ('vendor_exact', 'Vendor-Scoped Exact'),
                    ('vendor_fuzzy', 'Vendor-Scoped Fuzzy'),
                    ('fuzzy', 'Fuzzy Match'),
                    ('stripped_fuzzy', 'Stripped Prefix Fuzzy'),
                    ('keyword_batch', 'Keyword Batch (Human)'),
                    ('manual_review', 'Manual Review (Human)'),
                    ('unmatched', 'Unmatched'),
                ],
            ),
        ),
    ]
