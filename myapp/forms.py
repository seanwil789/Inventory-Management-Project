from django import forms
from django.forms import inlineformset_factory
from .models import Menu, Recipe, RecipeIngredient, YieldReference, CONFLICT_CHOICES


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

    class Meta:
        model = Recipe
        fields = ['name', 'yield_servings', 'notes', 'conflicts']
        widgets = {
            'name':           forms.TextInput(attrs={'class': 'border rounded px-2 py-1 w-full'}),
            'yield_servings': forms.NumberInput(attrs={'class': 'border rounded px-2 py-1 w-32'}),
            'notes':          forms.Textarea(attrs={'rows': 6, 'class': 'border rounded px-2 py-1 w-full font-mono text-sm'}),
        }


RecipeIngredientFormSet = inlineformset_factory(
    Recipe, RecipeIngredient,
    fk_name='recipe',
    fields=['name_raw', 'quantity', 'unit', 'yield_pct', 'yield_ref', 'sub_recipe'],
    extra=2,
    can_delete=True,
    widgets={
        'name_raw':   forms.TextInput(attrs={'class': 'border rounded px-2 py-1 w-full text-sm'}),
        'quantity':   forms.NumberInput(attrs={'class': 'border rounded px-2 py-1 w-full text-sm', 'step': '0.001'}),
        'unit':       forms.TextInput(attrs={'class': 'border rounded px-2 py-1 w-full text-sm'}),
        'yield_pct':  forms.NumberInput(attrs={'class': 'border rounded px-2 py-1 w-full text-sm', 'step': '0.01', 'placeholder': '%'}),
        'yield_ref':  forms.Select(attrs={'class': 'border rounded px-2 py-1 w-full text-sm'}),
        'sub_recipe': forms.Select(attrs={'class': 'border rounded px-2 py-1 w-full text-sm'}),
    },
)


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
