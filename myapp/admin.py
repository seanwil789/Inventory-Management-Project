from django.contrib import admin
from .models import Vendor, Product, ProductMapping, InvoiceLineItem


@admin.register(Vendor)
class VendorAdmin(admin.ModelAdmin):
    list_display  = ('name',)
    search_fields = ('name',)


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display   = ('canonical_name', 'category', 'primary_descriptor', 'secondary_descriptor')
    list_filter    = ('category', 'primary_descriptor')
    search_fields  = ('canonical_name',)
    ordering       = ('category', 'canonical_name')


@admin.register(ProductMapping)
class ProductMappingAdmin(admin.ModelAdmin):
    list_display   = ('description', 'supc', 'vendor', 'product')
    list_filter    = ('vendor',)
    search_fields  = ('description', 'supc', 'product__canonical_name')
    autocomplete_fields = ('product',)


@admin.register(InvoiceLineItem)
class InvoiceLineItemAdmin(admin.ModelAdmin):
    list_display   = ('invoice_date', 'vendor', 'product', 'raw_description', 'unit_price', 'case_size', 'source_file')
    list_filter    = ('vendor', 'invoice_date', 'product__category')
    search_fields  = ('product__canonical_name', 'raw_description', 'source_file')
    date_hierarchy = 'invoice_date'
    ordering       = ('-invoice_date',)
