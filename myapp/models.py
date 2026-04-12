from django.db import models


class Vendor(models.Model):
    name = models.CharField(max_length=120, unique=True)

    def __str__(self):
        return self.name


class Product(models.Model):
    """Canonical product — mirrors Item Mapping col F + category columns."""
    canonical_name       = models.CharField(max_length=200, unique=True)
    category             = models.CharField(max_length=100, blank=True)
    primary_descriptor   = models.CharField(max_length=100, blank=True)
    secondary_descriptor = models.CharField(max_length=100, blank=True)

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


class InvoiceLineItem(models.Model):
    """
    Single line item from a processed invoice.
    Replaces the Data Sheets transaction log.
    """
    vendor          = models.ForeignKey(Vendor, null=True, blank=True, on_delete=models.SET_NULL)
    product         = models.ForeignKey(Product, null=True, blank=True, on_delete=models.SET_NULL)
    raw_description = models.CharField(max_length=500, blank=True)  # fallback if unmatched
    unit_price      = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    case_size       = models.CharField(max_length=100, blank=True)
    invoice_date    = models.DateField(null=True, blank=True)
    source_file     = models.CharField(max_length=255, blank=True)  # original filename
    imported_at     = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['vendor', 'invoice_date']),
            models.Index(fields=['product', 'invoice_date']),
        ]

    def __str__(self):
        name = self.product.canonical_name if self.product else self.raw_description
        return f"{self.invoice_date} | {self.vendor} | {name} | ${self.unit_price}"
