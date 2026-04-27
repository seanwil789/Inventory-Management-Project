from django import forms
from django.forms import inlineformset_factory
from django.utils.safestring import mark_safe
from django.utils.html import escape
from .models import (Menu, Recipe, RecipeIngredient, YieldReference,
                     ProductMapping, CONFLICT_CHOICES)


class SharedDatalistWidget(forms.Widget):
    """Renders as visible text input + hidden PK input, both referencing a
    shared <datalist> declared once in the template. Eliminates the N×M
    <option> bloat of rendering a Select per row in an inline formset.

    On POST, only the hidden input is submitted under `name`; a small
    client-side hook (see recipe_form.html) resolves the visible typed
    value to a PK via the shared datalist.

    The visible initial label is populated on page load by the same hook,
    looking up value→label in the datalist — no DB queries per widget.
    """
    input_type = 'text'
    needs_multipart_form = False

    def __init__(self, datalist_id, attrs=None):
        self.datalist_id = datalist_id
        super().__init__(attrs)

    def value_from_datadict(self, data, files, name):
        return data.get(name)

    def render(self, name, value, attrs=None, renderer=None):
        attrs = self.build_attrs(attrs or {})
        css = escape(attrs.pop('class', 'border rounded px-2 py-1 w-full text-sm'))
        hidden_id = attrs.get('id') or f'id_{name}'
        vis_id = f'{hidden_id}_label'
        pk_val = '' if value in (None, '') else escape(str(value))
        return mark_safe(
            f'<input type="text" id="{vis_id}" list="{self.datalist_id}" '
            f'class="{css}" data-dl-pk-target="{hidden_id}" '
            f'data-dl-list="{self.datalist_id}" autocomplete="off">'
            f'<input type="hidden" name="{name}" id="{hidden_id}" '
            f'value="{pk_val}">'
        )


class ConflictsField(forms.MultipleChoiceField):
    """Render Recipe.conflicts (JSONField(default=list)) as a multi-select of
    Big-15 checkboxes. Stores as a list of string keys."""
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('choices', CONFLICT_CHOICES)
        kwargs.setdefault('widget', forms.CheckboxSelectMultiple(
            attrs={'class': 'grid grid-cols-2 sm:grid-cols-3 gap-1 text-sm'}))
        kwargs.setdefault('required', False)
        super().__init__(*args, **kwargs)

    def prepare_value(self, value):
        if value is None:
            return []
        return value


class ValidSlotsField(forms.MultipleChoiceField):
    """Render Recipe.valid_slots (JSONField(default=list)) as checkboxes of
    the four meal-slot keys. Empty selection = any slot (permissive)."""
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('choices', Menu.MEAL_SLOTS)
        kwargs.setdefault('widget', forms.CheckboxSelectMultiple(
            attrs={'class': 'grid grid-cols-2 sm:grid-cols-4 gap-1 text-sm'}))
        kwargs.setdefault('required', False)
        super().__init__(*args, **kwargs)

    def prepare_value(self, value):
        if value is None:
            return []
        return value


class MenuForm(forms.ModelForm):
    """Meal-name-first menu form. Components (linked recipes + freetext) are handled
    in the view via POST arrays, not on this ModelForm.
    """
    class Meta:
        model = Menu
        fields = ['dish_freetext', 'date', 'meal_slot', 'assignee']
        labels = {
            'dish_freetext': 'Meal name',
        }
        help_texts = {
            'dish_freetext': "How you'd describe the meal — e.g., 'Shrimp and Grits with fried eggs and tomato gravy'.",
        }
        widgets = {
            'date':          forms.DateInput(attrs={'type': 'date', 'class': 'border rounded px-2 py-1'}),
            'meal_slot':     forms.Select(attrs={'class': 'border rounded px-2 py-1'}),
            'dish_freetext': forms.TextInput(attrs={
                'class': 'border rounded px-2 py-1 w-full text-lg',
                'placeholder': 'Meal name (required)',
                'autofocus': True,
            }),
            'assignee':      forms.Select(attrs={'class': 'border rounded px-2 py-1'}),
        }

    def clean_dish_freetext(self):
        v = (self.cleaned_data.get('dish_freetext') or '').strip()
        if not v:
            raise forms.ValidationError("Meal name is required.")
        return v


class RecipeForm(forms.ModelForm):
    conflicts = ConflictsField(
        label='Dietary conflicts',
        help_text='What this recipe CONTAINS — e.g., a gluten-allergy client would avoid any recipe tagged gluten.',
    )
    valid_slots = ValidSlotsField(
        label='Menu slots',
        help_text='Which menu slots this recipe belongs in. Leave all unchecked to allow any slot.',
    )

    class Meta:
        model = Recipe
        fields = ['name', 'yield_servings', 'notes', 'conflicts', 'valid_slots']
        widgets = {
            'name':           forms.TextInput(attrs={'class': 'border rounded px-2 py-1 w-full'}),
            'yield_servings': forms.NumberInput(attrs={'class': 'border rounded px-2 py-1 w-32'}),
            'notes':          forms.Textarea(attrs={'rows': 6, 'class': 'border rounded px-2 py-1 w-full font-mono text-sm'}),
        }


RecipeIngredientFormSet = inlineformset_factory(
    Recipe, RecipeIngredient,
    fk_name='recipe',
    fields=['name_raw', 'quantity', 'unit', 'yield_pct', 'yield_ref',
            'sub_recipe', 'product'],
    extra=2,
    can_delete=True,
    widgets={
        # name_raw doubles as the canonical-product autocomplete trigger:
        # when the typed value matches a Product canonical_name, JS in
        # recipe_form.html populates the hidden `product` input below
        # so the FK gets saved on submit. Typing freetext that doesn't
        # match a canonical leaves product=NULL — same as before.
        'name_raw':   forms.TextInput(attrs={
            'class': 'border rounded px-2 py-1 w-full text-sm',
            'list':  'dl-products',
            'autocomplete': 'off',
        }),
        'quantity':   forms.NumberInput(attrs={'class': 'border rounded px-2 py-1 w-full text-sm', 'step': '0.001'}),
        'unit':       forms.TextInput(attrs={'class': 'border rounded px-2 py-1 w-full text-sm'}),
        'yield_pct':  forms.NumberInput(attrs={'class': 'border rounded px-2 py-1 w-full text-sm', 'step': '0.01', 'placeholder': '%'}),
        'yield_ref':  SharedDatalistWidget(datalist_id='dl-yield-refs'),
        'sub_recipe': SharedDatalistWidget(datalist_id='dl-sub-recipes'),
        'product':    forms.HiddenInput(),
    },
)


class ProductMappingForm(forms.ModelForm):
    """Edit / create form for a single ProductMapping row.

    Per the sheet→DB migration roadmap (Step 4): replaces direct edits
    of the Item Mapping sheet with a Django UI. The DB ProductMapping
    table is the source of truth (mapper reads it via Step 2 refactor),
    so saves here propagate to the live mapper on next cache refresh
    (1hr TTL). Surfacing through this form means errors land in the
    Django form-validation layer instead of silently dropping into the
    mapper's "skip orphan canonical" branch.
    """
    class Meta:
        model = ProductMapping
        fields = ['vendor', 'description', 'supc', 'product']
        widgets = {
            'vendor': forms.Select(attrs={'class': 'border rounded px-2 py-1 text-sm w-full'}),
            'description': forms.TextInput(attrs={
                'class': 'border rounded px-2 py-1 text-sm w-full font-mono',
                'placeholder': 'Raw vendor description (matches invoice OCR)',
            }),
            'supc': forms.TextInput(attrs={
                'class': 'border rounded px-2 py-1 text-sm w-full',
                'placeholder': 'Sysco SUPC (optional)',
            }),
            'product': forms.Select(attrs={'class': 'border rounded px-2 py-1 text-sm w-full'}),
        }


class YieldReferenceForm(forms.ModelForm):
    class Meta:
        model = YieldReference
        fields = ['ingredient', 'prep_state', 'section', 'yield_pct',
                  'ap_unit', 'ap_weight_oz', 'trimmed_unit', 'trimmed_weight_oz',
                  'trimmed_count', 'measures_per_ap', 'ounce_weight_per_cup',
                  'source', 'source_ref', 'notes', 'product']
        widgets = {
            'ingredient':        forms.TextInput(attrs={'class': 'border rounded px-2 py-1 w-full'}),
            'prep_state':        forms.TextInput(attrs={'class': 'border rounded px-2 py-1 w-full'}),
            'section':           forms.Select(attrs={'class': 'border rounded px-2 py-1'}),
            'yield_pct':         forms.NumberInput(attrs={'class': 'border rounded px-2 py-1 w-32', 'step': '0.01'}),
            'ap_unit':           forms.TextInput(attrs={'class': 'border rounded px-2 py-1 w-32'}),
            'ap_weight_oz':      forms.NumberInput(attrs={'class': 'border rounded px-2 py-1 w-32', 'step': '0.01'}),
            'trimmed_unit':      forms.TextInput(attrs={'class': 'border rounded px-2 py-1 w-32'}),
            'trimmed_weight_oz': forms.NumberInput(attrs={'class': 'border rounded px-2 py-1 w-32', 'step': '0.01'}),
            'trimmed_count':     forms.NumberInput(attrs={'class': 'border rounded px-2 py-1 w-32'}),
            'measures_per_ap':   forms.NumberInput(attrs={'class': 'border rounded px-2 py-1 w-32', 'step': '0.001'}),
            'ounce_weight_per_cup': forms.NumberInput(attrs={'class': 'border rounded px-2 py-1 w-32', 'step': '0.001'}),
            'source':            forms.TextInput(attrs={'class': 'border rounded px-2 py-1 w-64'}),
            'source_ref':        forms.TextInput(attrs={'class': 'border rounded px-2 py-1 w-32', 'placeholder': 'p.27'}),
            'notes':             forms.Textarea(attrs={'class': 'border rounded px-2 py-1 w-full', 'rows': 3}),
            'product':           forms.Select(attrs={'class': 'border rounded px-2 py-1'}),
        }
