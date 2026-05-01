"""Retire Taco Meat + Meatball (Pseudo) + rename Shrimp 21/25.

Per Sean (2026-04-30):
- Taco Meat (#196) → retire (Sean: "taco meat can be retired")
- Meatball (#200)  → retire (Sean: "meatballs can be retired")
- Shrimp 21/25 (#439) → rename to 'Shrimp, 21/25' (comma convention; Sean
  confirmed shrimp count grades stay as separate canonicals because
  "the price differential is so wide")
"""
from django.db import migrations


def retires_and_shrimp_rename(apps, schema_editor):
    Product = apps.get_model("myapp", "Product")
    # Retire to Pseudo (preserves history)
    Product.objects.filter(id=196, canonical_name="Taco Meat").update(category="Pseudo")
    Product.objects.filter(id=200, canonical_name="Meatball").update(category="Pseudo")
    # Rename Shrimp 21/25 to comma convention
    if not Product.objects.filter(canonical_name="Shrimp, 21/25").exists():
        Product.objects.filter(id=439, canonical_name="Shrimp 21/25").update(
            canonical_name="Shrimp, 21/25",
        )


def reverse_retires(apps, schema_editor):
    Product = apps.get_model("myapp", "Product")
    Product.objects.filter(id__in=(196, 200)).update(category="Proteins")
    Product.objects.filter(id=439, canonical_name="Shrimp, 21/25").update(
        canonical_name="Shrimp 21/25",
    )


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0047_proteins_plant_under_game"),
    ]

    operations = [
        migrations.RunPython(retires_and_shrimp_rename, reverse_retires),
    ]
