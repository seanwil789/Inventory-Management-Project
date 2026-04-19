from django.contrib import admin
from .models import (
    Vendor, Product, ProductMapping, InvoiceLineItem,
    Recipe, RecipeIngredient, Menu, PrepTask, Census,
    IngredientSkipNote, YieldReference,
)


@admin.register(YieldReference)
class YieldReferenceAdmin(admin.ModelAdmin):
    list_display  = ('ingredient', 'prep_state', 'section', 'yield_pct',
                     'ap_unit', 'ap_weight_oz', 'trimmed_weight_oz', 'source_ref')
    list_filter   = ('section', 'source', 'ap_unit')
    search_fields = ('ingredient', 'prep_state', 'notes')
    ordering      = ('section', 'ingredient', 'prep_state')
    autocomplete_fields = ('product',)


@admin.register(IngredientSkipNote)
class IngredientSkipNoteAdmin(admin.ModelAdmin):
    list_display  = ('name_raw', 'reason', 'created_at')
    search_fields = ('name_raw', 'reason')
    ordering      = ('-created_at',)


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


class RecipeIngredientInline(admin.TabularInline):
    model = RecipeIngredient
    fk_name = 'recipe'
    extra = 1
    fields = ('name_raw', 'quantity', 'unit', 'yield_pct', 'sub_recipe', 'product')
    autocomplete_fields = ('product', 'sub_recipe')


@admin.register(Recipe)
class RecipeAdmin(admin.ModelAdmin):
    list_display  = ('name', 'yield_servings', 'ingredient_count', 'is_composed', 'source_doc')
    search_fields = ('name',)
    inlines       = [RecipeIngredientInline]

    def ingredient_count(self, obj):
        return obj.ingredients.count()

    def is_composed(self, obj):
        return obj.ingredients.filter(sub_recipe__isnull=False).exists()
    is_composed.boolean = True


@admin.register(Menu)
class MenuAdmin(admin.ModelAdmin):
    list_display   = ('date', 'meal_slot', 'recipe', 'dish_freetext', 'assignee')
    list_filter    = ('meal_slot', 'assignee')
    search_fields  = ('dish_freetext', 'ingredients_raw')
    date_hierarchy = 'date'
    ordering       = ('-date', 'meal_slot')
    autocomplete_fields = ('recipe',)


@admin.register(PrepTask)
class PrepTaskAdmin(admin.ModelAdmin):
    list_display   = ('date', 'recipe', 'assignee', 'completed')
    list_filter    = ('assignee', 'completed')
    date_hierarchy = 'date'
    ordering       = ('-date',)
    autocomplete_fields = ('recipe',)


@admin.register(Census)
class CensusAdmin(admin.ModelAdmin):
    list_display   = ('date', 'headcount', 'notes')
    date_hierarchy = 'date'
    ordering       = ('-date',)
