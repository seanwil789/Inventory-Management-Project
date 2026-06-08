from django.db import migrations


SEED = [
    # name, qb_class, is_default, is_food_budget, sort_order
    ('Food/Kitchen',       'Food',               True,  True,  0),
    ('Operations',         'Operations',         False, False, 1),
    ('Coffee/Concessions', 'Coffee/Concessions', False, False, 2),
]


def seed_and_backfill(apps, schema_editor):
    Account = apps.get_model('myapp', 'Account')
    ILI = apps.get_model('myapp', 'InvoiceLineItem')
    accounts = {}
    for name, qb, is_def, is_food, order in SEED:
        acc, _ = Account.objects.get_or_create(
            name=name,
            defaults=dict(qb_class=qb, is_default=is_def,
                          is_food_budget=is_food, sort_order=order))
        accounts[name] = acc
    # Backfill every existing line item to the Food/Kitchen default so nothing
    # changes until invoices are explicitly tagged.
    food = accounts['Food/Kitchen']
    ILI.objects.filter(account__isnull=True).update(account=food)


def unseed(apps, schema_editor):
    Account = apps.get_model('myapp', 'Account')
    ILI = apps.get_model('myapp', 'InvoiceLineItem')
    ILI.objects.update(account=None)
    Account.objects.filter(
        name__in=[s[0] for s in SEED]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0076_account_invoicelineitem_account'),
    ]

    operations = [
        migrations.RunPython(seed_and_backfill, unseed),
    ]
