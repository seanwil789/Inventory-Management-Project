from datetime import date, timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.db import models
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .calendar_utils import (
    biweekly_start_for, MEAL_SLOT_LABELS, WEEKDAY_LABELS,
)
from collections import defaultdict

from .forms import MenuForm, RecipeForm, RecipeIngredientFormSet, YieldReferenceForm
from .models import (
    Census, IngredientSkipNote, InvoiceLineItem, Menu, MenuFreetextComponent,
    PrepTask, Product, Recipe, RecipeIngredient, Vendor, YieldReference,
    PROTEIN_CHOICES, CONFLICT_LABELS, CONFLICT_ICONS,
)


PROTEIN_ORDER = ['beef', 'chicken', 'pork', 'turkey', 'seafood', 'veg', 'eggs', 'other', '']
PROTEIN_LABELS = dict(PROTEIN_CHOICES)
PROTEIN_LABELS[''] = 'Unspecified'


def _recipes_by_protein():
    """Return [(label, [recipes])] ordered for the picker optgroups."""
    groups: dict[str, list[Recipe]] = defaultdict(list)
    for r in Recipe.objects.order_by('name'):
        groups[r.protein or ''].append(r)
    out = []
    for key in PROTEIN_ORDER:
        if key in groups:
            out.append((PROTEIN_LABELS.get(key, key.title()), groups[key]))
    return out


def _parse_iso(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise Http404(f"Bad date: {s}")


def _dominant_protein(menu) -> str:
    """Most-common protein across a menu's linked recipes, '' if none."""
    from collections import Counter
    proteins = [r.protein for r in menu.additional_recipes.all() if r.protein]
    if not proteins:
        return ''
    return Counter(proteins).most_common(1)[0][0]


def _menu_conflict_set(menu: Menu) -> list[tuple[str, str, str]]:
    """Union of dietary conflict flags across menu.recipe + additional_recipes.
    Returns sorted [(key, label, icon), ...] for template rendering."""
    keys: set[str] = set()
    if menu.recipe and menu.recipe.conflicts:
        keys.update(menu.recipe.conflicts)
    for r in menu.additional_recipes.all():
        if r.conflicts:
            keys.update(r.conflicts)
    return [(k, CONFLICT_LABELS.get(k, k), CONFLICT_ICONS.get(k, '?'))
            for k in sorted(keys)]


def _menu_cost(menu: Menu, headcount: int | None) -> Decimal | None:
    """Sum cost of main + additional recipes × headcount. None if no priced data."""
    if not menu or not headcount:
        return None
    recipes = []
    if menu.recipe:
        recipes.append(menu.recipe)
    recipes.extend(menu.additional_recipes.all())
    if not recipes:
        return None
    total = Decimal('0')
    had_cost = False
    for r in recipes:
        c = r.cost_for_headcount(headcount)
        if c is not None:
            total += c
            had_cost = True
    return total if had_cost else None


def _build_week(start: date, menu_map, census_map, protein_map):
    """protein_map: {(date, slot): protein_str}"""
    days = [start + timedelta(days=i) for i in range(5)]  # Mon-Fri
    rows = []
    for slot_key, slot_label in MEAL_SLOT_LABELS:
        cells = []
        for day in days:
            menu = menu_map.get((day, slot_key))
            census = census_map.get(day)
            headcount = census.headcount if census else None
            cost = _menu_cost(menu, headcount) if menu else None
            conflicts = _menu_conflict_set(menu) if menu else []
            cell = {'date': day, 'slot': slot_key, 'menu': menu, 'warnings': [],
                    'cost': cost, 'menu_conflicts': conflicts}
            if menu:
                mine = protein_map.get((day, slot_key), '')
                if mine:
                    # Rule 1: same protein as yesterday's dinner (only check on dinner slot)
                    if slot_key == 'dinner':
                        yest = protein_map.get((day - timedelta(days=1), 'dinner'), '')
                        if yest and yest == mine:
                            cell['warnings'].append(('red', f"Same protein ({mine}) as yesterday's dinner"))
                    # Rule 2: same protein as today's lunch (dinner check)
                    if slot_key == 'dinner':
                        lunch = protein_map.get((day, 'lunch'), '')
                        if lunch and lunch == mine:
                            cell['warnings'].append(('yellow', f"Same protein ({mine}) as today's lunch"))
                    # Rule 2b: lunch slot — also warn if same as today's dinner
                    if slot_key == 'lunch':
                        dinner = protein_map.get((day, 'dinner'), '')
                        if dinner and dinner == mine:
                            cell['warnings'].append(('yellow', f"Same protein ({mine}) as today's dinner"))
            cells.append(cell)
        rows.append({'slot_key': slot_key, 'slot_label': slot_label, 'cells': cells})
    # Per-day totals: sum of cost across all slots on that day
    day_totals = []
    for i, d in enumerate(days):
        t = Decimal('0')
        any_priced = False
        for row in rows:
            c = row['cells'][i]['cost']
            if c is not None:
                t += c
                any_priced = True
        day_totals.append(t if any_priced else None)

    week_total = sum((t for t in day_totals if t is not None), Decimal('0')) or None

    day_headers = [{
        'date': d,
        'label': WEEKDAY_LABELS[i],
        'census': census_map.get(d),
        'cost':   day_totals[i],
    } for i, d in enumerate(days)]
    # Per-day stack for mobile layout: each day gets all 4 meal slots inline
    mobile_days = []
    for i, d in enumerate(days):
        mobile_days.append({
            'header': day_headers[i],
            'slots':  [(row['slot_label'], row['cells'][i]) for row in rows],
        })
    return {'start': start, 'day_headers': day_headers, 'rows': rows,
            'mobile_days': mobile_days, 'week_total': week_total}


def calendar_current(request):
    return calendar_biweekly(request, date.today().isoformat())


def calendar_biweekly(request, start_date_str: str):
    target = _parse_iso(start_date_str)
    bw_start = biweekly_start_for(target)
    bw_end   = bw_start + timedelta(days=13)

    # Load a day wider on each side so warnings can look at adjacent-day dinners
    menus = (Menu.objects
             .filter(date__gte=bw_start - timedelta(days=1), date__lte=bw_end + timedelta(days=1))
             .prefetch_related('additional_recipes'))
    menu_map = {(m.date, m.meal_slot): m for m in menus if bw_start <= m.date <= bw_end}
    protein_map = {(m.date, m.meal_slot): _dominant_protein(m) for m in menus}
    census_map = {c.date: c for c in Census.objects.filter(date__gte=bw_start, date__lte=bw_end)}

    week1 = _build_week(bw_start, menu_map, census_map, protein_map)
    week2 = _build_week(bw_start + timedelta(days=7), menu_map, census_map, protein_map)

    return render(request, 'myapp/calendar.html', {
        'biweekly_start': bw_start,
        'biweekly_end':   bw_end,
        'weeks':          [week1, week2],
        'prev_biweekly':  bw_start - timedelta(days=14),
        'next_biweekly':  bw_start + timedelta(days=14),
        'today':          date.today(),
    })


def menu_detail(request, menu_id: int):
    from .models import MealService
    menu = get_object_or_404(Menu, pk=menu_id)
    services = menu.service_records.all().order_by('-created_at')
    return render(request, 'myapp/menu_detail.html', {
        'menu': menu,
        'services': services,
    })


@require_POST
def menu_log_service(request, menu_id: int):
    """Create a MealService record for this menu (cleanup touchpoint)."""
    from .models import MealService
    from decimal import Decimal, InvalidOperation

    menu = get_object_or_404(Menu, pk=menu_id)

    def _dec(name):
        v = (request.POST.get(name) or '').strip()
        if not v:
            return None
        try:
            return Decimal(v)
        except InvalidOperation:
            return None

    prepped = _dec('prepped_qty')
    leftover = _dec('post_service_leftover_qty')
    unit = (request.POST.get('unit') or '').strip()[:30]
    notes = (request.POST.get('notes') or '').strip()

    if prepped is None:
        messages.error(request, "Prepped quantity is required.")
        return redirect(reverse('menu_detail', args=[menu.id]))

    MealService.objects.create(
        menu=menu,
        prepped_qty=prepped,
        post_service_leftover_qty=leftover,
        unit=unit,
        notes=notes,
    )
    messages.success(request,
        f"Logged service: prepped {prepped} {unit}"
        + (f", leftover {leftover} {unit}" if leftover is not None else ""))
    return redirect(reverse('menu_detail', args=[menu.id]))


@require_POST
def mealservice_log_disposal(request, service_id: int):
    """Update a MealService with disposal info (day-5 touchpoint)."""
    from .models import MealService
    from datetime import datetime
    from decimal import Decimal, InvalidOperation

    svc = get_object_or_404(MealService, pk=service_id)
    raw = (request.POST.get('discarded_qty') or '').strip()
    try:
        svc.discarded_qty = Decimal(raw) if raw else None
    except InvalidOperation:
        messages.error(request, f"Invalid discarded quantity: {raw}")
        return redirect(reverse('menu_detail', args=[svc.menu_id]))

    if svc.discarded_qty is not None:
        svc.discarded_at = datetime.now()
    svc.save(update_fields=['discarded_qty', 'discarded_at', 'updated_at'])
    messages.success(request,
        f"Logged disposal: {svc.discarded_qty} {svc.unit}" if svc.discarded_qty
        else "Cleared disposal record.")
    return redirect(reverse('menu_detail', args=[svc.menu_id]))


def _save_components(menu: Menu, post) -> None:
    """Parse the 4 parallel POST arrays from component rows:
      component_recipe_id[]     → a linked recipe
      component_freetext_name[] → structured freetext name
      component_freetext_qty[]  → qty (decimal-ish string)
      component_freetext_unit[] → unit string
    Each row contributes either the linked recipe OR the freetext (if name is set).
    """
    recipe_ids = post.getlist('component_recipe_id')
    ft_names   = post.getlist('component_freetext_name')
    ft_qtys    = post.getlist('component_freetext_qty')
    ft_units   = post.getlist('component_freetext_unit')
    rows = max(len(recipe_ids), len(ft_names), len(ft_qtys), len(ft_units))

    picked: list[Recipe] = []
    picked_ids: set[int] = set()
    freetext_rows: list[tuple[str, Decimal | None, str]] = []

    for i in range(rows):
        rid       = recipe_ids[i] if i < len(recipe_ids) else ''
        ft_name   = (ft_names[i]  if i < len(ft_names)  else '').strip()
        ft_qty_s  = (ft_qtys[i]   if i < len(ft_qtys)   else '').strip()
        ft_unit   = (ft_units[i]  if i < len(ft_units)  else '').strip()
        if rid:
            try:
                r = Recipe.objects.get(pk=int(rid))
                if r.id not in picked_ids:
                    picked.append(r)
                    picked_ids.add(r.id)
            except (Recipe.DoesNotExist, ValueError):
                pass
        elif ft_name:
            try:
                ft_qty = Decimal(ft_qty_s) if ft_qty_s else None
            except Exception:
                ft_qty = None
            freetext_rows.append((ft_name, ft_qty, ft_unit))

    menu.additional_recipes.set(picked)
    # Replace freetext components wholesale (simpler than diffing)
    menu.freetext_components.all().delete()
    for pos, (name, qty, unit) in enumerate(freetext_rows):
        MenuFreetextComponent.objects.create(
            menu=menu, name=name[:200], quantity=qty, unit=unit[:30], position=pos,
        )
    # Clear legacy ingredients_raw — new flow uses structured rows.
    if menu.ingredients_raw:
        menu.ingredients_raw = ''
        menu.save(update_fields=['ingredients_raw'])


def _recipes_for_slot(slot: str | None):
    """Recipes valid for a given meal-slot: those whose valid_slots is empty
    (permissive/any-slot) OR explicitly contains the slot. Falls back to all
    recipes when slot is None.

    Python-side filter because SQLite's JSONField doesn't support __contains
    lookups on array elements. With ~80 recipes this is negligible."""
    qs = Recipe.objects.order_by('name')
    if not slot:
        return qs
    return [r for r in qs if not r.valid_slots or slot in r.valid_slots]


def menu_edit(request, menu_id: int):
    menu = get_object_or_404(Menu, pk=menu_id)
    if request.method == 'POST':
        form = MenuForm(request.POST, instance=menu)
        if form.is_valid():
            form.save()
            _save_components(menu, request.POST)
            return redirect(reverse('calendar_biweekly', args=[menu.date.isoformat()]))
    else:
        form = MenuForm(instance=menu)
    return render(request, 'myapp/menu_form.html', {
        'form':          form,
        'menu':          menu,
        'title':         f"Edit {menu.get_meal_slot_display()} — {menu.date}",
        'all_recipes':   _recipes_for_slot(menu.meal_slot),
        'recipes_by_protein': _recipes_by_protein(),
        'initial_components': _initial_component_rows(menu),
    })


def menu_new(request):
    """Create a Menu row. Expects ?date=YYYY-MM-DD&slot=cold_breakfast in GET."""
    initial = {}
    d = request.GET.get('date')
    slot = request.GET.get('slot')
    if d:
        initial['date'] = d
    if slot:
        initial['meal_slot'] = slot

    if request.method == 'POST':
        form = MenuForm(request.POST)
        if form.is_valid():
            menu = form.save()
            _save_components(menu, request.POST)
            return redirect(reverse('calendar_biweekly', args=[menu.date.isoformat()]))
    else:
        form = MenuForm(initial=initial)
    return render(request, 'myapp/menu_form.html', {
        'form':          form,
        'menu':          None,
        'title':         "Add meal",
        'all_recipes':   _recipes_for_slot(slot),
        'recipes_by_protein': _recipes_by_protein(),
        'initial_components': [],
    })


def _empty_row() -> dict:
    return {
        'recipe_id': '', 'recipe_name': '',
        'freetext_name': '', 'freetext_qty': '', 'freetext_unit': '',
    }


def _initial_component_rows(menu: Menu) -> list[dict]:
    """Prefill the component list when editing."""
    rows: list[dict] = []
    existing_ids = set(menu.additional_recipes.values_list('id', flat=True))
    if menu.recipe_id and menu.recipe_id not in existing_ids:
        rows.append({**_empty_row(), 'recipe_id': menu.recipe.id, 'recipe_name': menu.recipe.name})
    for r in menu.additional_recipes.all():
        rows.append({**_empty_row(), 'recipe_id': r.id, 'recipe_name': r.name})
    for fc in menu.freetext_components.all():
        rows.append({
            **_empty_row(),
            'freetext_name': fc.name,
            'freetext_qty':  str(fc.quantity) if fc.quantity is not None else '',
            'freetext_unit': fc.unit,
        })
    # Legacy: any old ingredients_raw lines (from prior flow) become freetext rows without qty
    for line in (menu.ingredients_raw or '').splitlines():
        line = line.strip()
        if line:
            rows.append({**_empty_row(), 'freetext_name': line})
    return rows


def _rows_from_recipe(recipe: Recipe) -> list[dict]:
    """Convert a Recipe's ingredients into component-row shape."""
    rows = []
    for ing in recipe.ingredients.all():
        if ing.sub_recipe:
            rows.append({**_empty_row(),
                         'recipe_id': ing.sub_recipe.id, 'recipe_name': ing.sub_recipe.name})
        elif ing.name_raw:
            rows.append({**_empty_row(),
                         'freetext_name': ing.name_raw,
                         'freetext_qty':  str(ing.quantity) if ing.quantity is not None else '',
                         'freetext_unit': ing.unit})
    return rows


def menu_component_row(request):
    """HTMX partial: blank component row."""
    return render(request, 'myapp/_component_row.html', {
        'row':         _empty_row(),
        'recipes_by_protein': _recipes_by_protein(),
    })


def menu_autofill_components(request):
    """HTMX endpoint: when meal name exactly matches a Recipe name, return its components.
    Otherwise return HX-Reswap: none so nothing is clobbered.
    """
    from django.http import HttpResponse
    name = (request.GET.get('dish_freetext') or '').strip()
    recipe = Recipe.objects.filter(name__iexact=name).first() if name else None
    if not recipe:
        resp = HttpResponse('', status=204)
        resp['HX-Reswap'] = 'none'
        return resp
    return render(request, 'myapp/_components_list.html', {
        'rows':        _rows_from_recipe(recipe),
        'recipes_by_protein': _recipes_by_protein(),
    })


def menu_delete(request, menu_id: int):
    menu = get_object_or_404(Menu, pk=menu_id)
    if request.method == 'POST':
        redirect_date = menu.date.isoformat()
        menu.delete()
        return redirect(reverse('calendar_biweekly', args=[redirect_date]))
    return render(request, 'myapp/menu_confirm_delete.html', {'menu': menu})


def _recipe_category(recipe: Recipe) -> str:
    """Derive a category label from the Recipe's source_doc path.

    'Recipe Book/Baking/Cookies and Bars/X.docx' → 'Baking'
    'Composed Meals/Taco Lasagna/'               → 'Composed Meals'
    empty / unrecognized                          → 'My Meals'
    """
    src = (recipe.source_doc or '').strip()
    if not src:
        return 'My Meals'
    parts = src.replace('\\', '/').split('/')
    # Skip a leading "Recipe Book" if present
    if parts and parts[0] == 'Recipe Book':
        parts = parts[1:]
    if not parts or not parts[0]:
        return 'My Meals'
    return parts[0]


def bridge_review(request):
    """Show unmatched RecipeIngredients grouped by name_raw, with candidate Products."""
    from rapidfuzz import fuzz, process

    show_skipped = request.GET.get('show_skipped') == '1'

    # All unmatched, unlinked-to-sub-recipe ingredients
    unmatched = (RecipeIngredient.objects
                 .filter(product__isnull=True, sub_recipe__isnull=True)
                 .select_related('recipe')
                 .order_by('name_raw'))

    # Skipped name_raws — hidden by default
    skipped_names = set(IngredientSkipNote.objects.values_list('name_raw', flat=True))
    skipped_names_lower = {s.lower().strip() for s in skipped_names}

    # Group by normalized name_raw
    from collections import defaultdict
    groups: dict[str, dict] = defaultdict(lambda: {'name_raw': '', 'count': 0, 'recipes': set(), 'any_qty': False})
    for ri in unmatched:
        key = ri.name_raw.strip().lower()
        if not show_skipped and key in skipped_names_lower:
            continue
        g = groups[key]
        g['name_raw'] = ri.name_raw
        g['count'] += 1
        g['recipes'].add(ri.recipe.name)
        if ri.quantity:
            g['any_qty'] = True

    # Compute top 3 candidates per group using rapidfuzz
    products = list(Product.objects.all())
    product_names = [p.canonical_name for p in products]

    groups_list = []
    for key, g in groups.items():
        candidates = process.extract(g['name_raw'], product_names, scorer=fuzz.WRatio, limit=3)
        candidate_objs = []
        for name, score, idx in candidates:
            if score >= 50:
                candidate_objs.append({'product': products[idx], 'score': int(score)})
        groups_list.append({
            'name_raw':   g['name_raw'],
            'count':      g['count'],
            'recipes':    sorted(g['recipes'])[:3],
            'more_recipes': max(0, len(g['recipes']) - 3),
            'candidates': candidate_objs,
        })

    # Sort: most-common first, within that by score
    groups_list.sort(key=lambda g: (-g['count'], g['name_raw'].lower()))

    return render(request, 'myapp/bridge_review.html', {
        'groups':       groups_list,
        'remaining':    unmatched.count(),
        'skipped_count': len(skipped_names),
        'show_skipped': show_skipped,
    })


@require_POST
def bridge_link(request):
    """Apply a product to every unmatched RecipeIngredient with a matching name_raw."""
    name_raw = (request.POST.get('name_raw') or '').strip()
    product_id = request.POST.get('product_id')
    if not name_raw or not product_id:
        return HttpResponseBadRequest("name_raw and product_id required")
    try:
        product = Product.objects.get(pk=int(product_id))
    except (Product.DoesNotExist, ValueError):
        return HttpResponseBadRequest("bad product_id")

    n = (RecipeIngredient.objects
         .filter(product__isnull=True, sub_recipe__isnull=True, name_raw__iexact=name_raw)
         .update(product=product))
    return render(request, 'myapp/_bridge_row_applied.html', {
        'name_raw': name_raw, 'product': product, 'count': n,
    })


@require_POST
def bridge_skip(request):
    """Persist a skip note (optional reason) so tomorrow's catalog pass has breadcrumbs."""
    name_raw = (request.POST.get('name_raw') or '').strip()
    reason   = (request.POST.get('reason') or '').strip()
    if name_raw:
        IngredientSkipNote.objects.update_or_create(
            name_raw=name_raw, defaults={'reason': reason[:300]},
        )
    return render(request, 'myapp/_bridge_row_skipped.html',
                  {'name_raw': name_raw, 'reason': reason})


def bridge_skipped(request):
    """Review all skipped ingredients — landing page for tomorrow's catalog pass."""
    notes = IngredientSkipNote.objects.order_by('name_raw')
    return render(request, 'myapp/bridge_skipped.html', {'notes': notes})


@require_POST
def bridge_unskip(request, note_id: int):
    """Remove a skip note so the ingredient reappears in the main review."""
    IngredientSkipNote.objects.filter(pk=note_id).delete()
    return HttpResponse('')  # HTMX: swap-out with empty replaces the row


def bridge_search_products(request):
    """HTMX: find products by substring for manual assignment."""
    q = (request.GET.get('q') or '').strip()
    name_raw = (request.GET.get('name_raw') or '').strip()
    products = []
    if len(q) >= 2:
        products = list(Product.objects.filter(canonical_name__icontains=q)
                        .order_by('canonical_name')[:10])
    return render(request, 'myapp/_bridge_search_results.html',
                  {'products': products, 'name_raw': name_raw})


def prep_list(request):
    """Show prep tasks grouped by date — today + next 7 days."""
    start = date.today()
    end   = start + timedelta(days=7)
    tasks = (PrepTask.objects
             .filter(date__gte=start, date__lte=end)
             .select_related('recipe')
             .order_by('date', 'recipe__name'))
    # Also include back-dated incomplete tasks (missed prep!)
    overdue = (PrepTask.objects
               .filter(date__lt=start, completed=False)
               .select_related('recipe')
               .order_by('date', 'recipe__name'))

    from collections import defaultdict
    by_date: dict[date, list[PrepTask]] = defaultdict(list)
    for t in tasks:
        by_date[t.date].append(t)
    date_groups = [(d, by_date[d]) for d in sorted(by_date.keys())]

    return render(request, 'myapp/prep_list.html', {
        'date_groups': date_groups,
        'overdue':     list(overdue),
        'today':       start,
        'window_end':  end,
    })


@require_POST
def preptask_toggle(request, task_id: int):
    """HTMX endpoint: flip completed status, return updated row HTML."""
    t = get_object_or_404(PrepTask, pk=task_id)
    t.completed = not t.completed
    t.save(update_fields=['completed'])
    return render(request, 'myapp/_preptask_row.html', {'t': t})


DEFAULT_CENSUS = 30        # fallback when no Census row exists for a date
DEFAULT_YIELD = 40         # fallback when recipe has no yield_servings
MAX_SUB_DEPTH = 4          # cap sub_recipe recursion to avoid cycles


def _expand_recipe(recipe: Recipe, scale: float, depth: int = 0) -> list[dict]:
    """Recursively walk a recipe's ingredients, scaling by `scale`.
    Returns a list of ingredient dicts with absolute quantities.
    Sub-recipes are recursed into; their ingredients scaled by parent's batch count × scale.
    Each ingredient dict:
        {product, name_raw, qty, unit}
    product may be None for un-linked RecipeIngredients.
    """
    if depth > MAX_SUB_DEPTH:
        return []
    out: list[dict] = []
    for ing in recipe.ingredients.all().select_related('product', 'sub_recipe'):
        if ing.sub_recipe_id:
            # sub-recipe: one 'batch' = scale-by-1 relative to parent at parent's scale
            sub_scale = scale * float(ing.quantity or 1)
            out.extend(_expand_recipe(ing.sub_recipe, sub_scale, depth + 1))
        else:
            scaled_qty = float(ing.quantity) * scale if ing.quantity else None
            out.append({
                'product':  ing.product,
                'name_raw': ing.name_raw,
                'qty':      scaled_qty,
                'unit':     ing.unit,
            })
    return out


def _latest_invoice_info(product):
    """Return (vendor, unit_price, case_size) from most-recent InvoiceLineItem.
    Single-product lookup. Use `_latest_invoice_info_bulk()` for loops."""
    from myapp.models import InvoiceLineItem
    latest = (InvoiceLineItem.objects
              .filter(product=product)
              .order_by('-invoice_date')
              .select_related('vendor')
              .first())
    if not latest:
        return None, None, None
    return latest.vendor, latest.unit_price, latest.case_size


class _VendorLite:
    """Lightweight vendor wrapper — gives `.name` attribute so callers
    that treat `vendor` as an object don't break. Used by bulk lookup
    since `.values()` doesn't hydrate full Vendor instances."""
    __slots__ = ('name',)
    def __init__(self, name):
        self.name = name


def _latest_invoice_info_bulk(product_ids):
    """Return {product_id: (vendor, unit_price, case_size, last_invoice_date)}
    for all products in one DB query. Replaces N queries in order_guide's
    vendor loop. `last_invoice_date` supports the "last ordered N days ago"
    stamp on order-guide rows."""
    if not product_ids:
        return {}
    rows = (InvoiceLineItem.objects
            .filter(product_id__in=product_ids)
            .order_by('-invoice_date', '-imported_at')
            .values('product_id', 'vendor__name', 'unit_price', 'case_size',
                    'invoice_date'))
    out = {}
    for r in rows:
        pid = r['product_id']
        if pid not in out:
            v = _VendorLite(r['vendor__name']) if r['vendor__name'] else None
            out[pid] = (v, r['unit_price'], r['case_size'], r['invoice_date'])
    return out


def _usage_pattern_predictions(as_of: date, min_purchases: int = 3,
                               urgency_threshold: float = 0.9):
    """Surface non-recipe products likely due for reorder based on purchase
    cadence. Non-recipe = no RecipeIngredient references this Product.

    Algorithm (from project_usage_pattern_order_guide.md):
      - Need min_purchases historical invoices to avoid noise.
      - avg_interval = mean days between consecutive purchases.
      - days_since_last = as_of − most-recent invoice_date.
      - urgency = days_since_last / avg_interval (>=1.0 = overdue).
      - Flag when urgency >= urgency_threshold (default 0.9).

    Returns list of dicts, sorted most-urgent first, for items past threshold.
    """
    from myapp.models import Product, RecipeIngredient, InvoiceLineItem
    from collections import defaultdict

    # Non-recipe products: any Product not referenced by a RecipeIngredient
    recipe_linked_ids = set(RecipeIngredient.objects
                            .exclude(product__isnull=True)
                            .values_list('product_id', flat=True))
    non_recipe_qs = Product.objects.exclude(id__in=recipe_linked_ids)

    # Pull invoice history in one query, newest first
    history_rows = (InvoiceLineItem.objects
                    .filter(product_id__in=non_recipe_qs.values_list('id', flat=True),
                            invoice_date__isnull=False)
                    .values('product_id', 'invoice_date', 'vendor__name',
                            'unit_price', 'case_size')
                    .order_by('product_id', 'invoice_date'))

    by_product: dict[int, list[dict]] = defaultdict(list)
    for r in history_rows:
        by_product[r['product_id']].append(r)

    product_lookup = {p.id: p for p in non_recipe_qs}
    predictions = []

    for pid, invoices in by_product.items():
        if len(invoices) < min_purchases:
            continue
        # Deduplicate same-day purchases (multi-line invoices count once)
        dates_seen: list[date] = []
        for inv in invoices:
            d = inv['invoice_date']
            if not dates_seen or d != dates_seen[-1]:
                dates_seen.append(d)
        if len(dates_seen) < min_purchases:
            continue
        intervals = [(dates_seen[i] - dates_seen[i-1]).days
                     for i in range(1, len(dates_seen))]
        if not intervals:
            continue
        avg_interval = sum(intervals) / len(intervals)
        if avg_interval <= 0:
            continue
        last_date = dates_seen[-1]
        days_since_last = (as_of - last_date).days
        urgency = days_since_last / avg_interval
        if urgency < urgency_threshold:
            continue
        last_inv = invoices[-1]
        product = product_lookup.get(pid)
        if not product:
            continue
        predictions.append({
            'product':         product,
            'vendor':          last_inv['vendor__name'] or '— unknown —',
            'unit_price':      last_inv['unit_price'],
            'case_size':       last_inv['case_size'],
            'last_date':       last_date,
            'days_since_last': days_since_last,
            'avg_interval':    round(avg_interval, 1),
            'urgency':         round(urgency, 2),
            'purchase_count':  len(dates_seen),
        })

    predictions.sort(key=lambda p: -p['urgency'])
    return predictions


def order_guide(request):
    """Aggregate ingredients needed across a date range of menus, grouped by vendor."""
    # Date range (default: today → today+6)
    today = date.today()
    try:
        start = date.fromisoformat(request.GET['start']) if request.GET.get('start') else today
        end   = date.fromisoformat(request.GET['end'])   if request.GET.get('end')   else start + timedelta(days=6)
    except ValueError:
        start, end = today, today + timedelta(days=6)

    # Census lookup for each day
    census_map = {c.date: c.headcount for c in Census.objects.filter(date__gte=start, date__lte=end)}
    # Fallback: use the most recent known census as default for dates without one
    recent_census = Census.objects.filter(date__lte=end).order_by('-date').first()
    default_headcount = recent_census.headcount if recent_census else DEFAULT_CENSUS

    # All menus in range with their linked recipes + freetext components
    menus = (Menu.objects
             .filter(date__gte=start, date__lte=end)
             .prefetch_related('additional_recipes', 'freetext_components', 'recipe'))

    # Aggregator: keyed by (product_id or name_raw+unit, unit)
    agg_by_product: dict[int, dict] = {}
    agg_by_name: dict[tuple[str, str], dict] = {}
    freetext_list: list[dict] = []
    unlinked_menus: list[Menu] = []
    included_menu_count = 0

    for menu in menus:
        headcount = census_map.get(menu.date, default_headcount)
        # Collect recipes (legacy FK + additional_recipes)
        recipes: list[Recipe] = list(menu.additional_recipes.all())
        if menu.recipe_id and menu.recipe not in recipes:
            recipes.append(menu.recipe)

        if not recipes and not menu.freetext_components.exists():
            unlinked_menus.append(menu)
            continue

        included_menu_count += 1

        # Expand each recipe
        for recipe in recipes:
            recipe_yield = recipe.yield_servings or DEFAULT_YIELD
            scale = headcount / recipe_yield
            for ing in _expand_recipe(recipe, scale):
                if ing['qty'] is None:
                    continue  # salt-to-taste etc.
                if ing['product']:
                    pid = ing['product'].id
                    if pid not in agg_by_product:
                        agg_by_product[pid] = {
                            'product': ing['product'],
                            'by_unit': {},
                            'contributors': set(),
                        }
                    u = ing['unit'] or ''
                    agg_by_product[pid]['by_unit'][u] = agg_by_product[pid]['by_unit'].get(u, 0.0) + ing['qty']
                    agg_by_product[pid]['contributors'].add(recipe.name)
                else:
                    key = (ing['name_raw'].lower().strip(), ing['unit'] or '')
                    if key not in agg_by_name:
                        agg_by_name[key] = {'name_raw': ing['name_raw'], 'unit': ing['unit'], 'qty': 0.0}
                    agg_by_name[key]['qty'] += ing['qty']

        # Menu-level freetext components (structured with qty/unit)
        for fc in menu.freetext_components.all():
            if fc.quantity:
                q = float(fc.quantity) * (headcount / DEFAULT_YIELD)  # rough scaling
            else:
                q = None
            freetext_list.append({
                'menu_date': menu.date, 'meal_slot': menu.meal_slot,
                'name': fc.name, 'qty': q, 'unit': fc.unit,
            })

    # Bucket products by vendor using latest invoice.
    # One bulk query instead of N: big win for menus with many ingredients.
    from collections import defaultdict
    latest_info = _latest_invoice_info_bulk(list(agg_by_product.keys()))
    by_vendor: dict[str, list[dict]] = defaultdict(list)
    for pid, data in agg_by_product.items():
        vendor, unit_price, case_size, last_date = latest_info.get(
            pid, (None, None, None, None))
        vendor_name = vendor.name if vendor else '— unknown / no invoice history —'
        days_since = (today - last_date).days if last_date else None
        for unit, qty in data['by_unit'].items():
            line_total = (float(unit_price) * qty) if unit_price else None
            by_vendor[vendor_name].append({
                'product':     data['product'],
                'qty':         qty,
                'unit':        unit,
                'unit_price':  unit_price,
                'line_total':  line_total,
                'contributors': sorted(data['contributors'])[:3],
                'case_size':   case_size,
                'last_date':   last_date,
                'days_since':  days_since,
            })

    # Sort vendor groups alphabetically; inside, by product name
    vendor_groups = []
    for vname in sorted(by_vendor.keys()):
        lines = sorted(by_vendor[vname], key=lambda r: r['product'].canonical_name)
        total = sum(l['line_total'] or 0 for l in lines)
        has_priced = any(l['line_total'] is not None for l in lines)
        vendor_groups.append({
            'vendor': vname, 'lines': lines, 'total': total, 'has_priced': has_priced,
        })

    # Name-based (product=None) stragglers
    unlinked_names = sorted(agg_by_name.values(), key=lambda r: r['name_raw'].lower())

    # Usage-pattern track: non-recipe products predicted due for reorder.
    # Grouped by vendor to mirror the recipe-driven section.
    usage_predictions = _usage_pattern_predictions(today)
    usage_by_vendor: dict[str, list[dict]] = defaultdict(list)
    for p in usage_predictions:
        usage_by_vendor[p['vendor']].append(p)
    usage_groups = [
        {'vendor': v, 'lines': sorted(lines, key=lambda r: -r['urgency'])}
        for v, lines in sorted(usage_by_vendor.items())
    ]

    return render(request, 'myapp/order_guide.html', {
        'start':            start,
        'end':              end,
        'prev_start':       start - timedelta(days=7),
        'next_start':       start + timedelta(days=7),
        'included_count':   included_menu_count,
        'unlinked_menus':   unlinked_menus,
        'vendor_groups':    vendor_groups,
        'unlinked_names':   unlinked_names,
        'freetext_list':    freetext_list,
        'default_headcount': default_headcount,
        'usage_groups':     usage_groups,
        'usage_total':      len(usage_predictions),
        'today':            today,
    })


@login_not_required
def kitchen_display(request):
    """Read-only, big-text kitchen display — designed for a wall-mounted tablet.
    Optional ?as_of=YYYY-MM-DD to preview any date (demo-friendly).

    Exempt from login via @login_not_required — the wall display has no user
    to authenticate and must render for anyone on the tailnet."""
    from datetime import datetime

    as_of_str = request.GET.get('as_of')
    try:
        as_of = date.fromisoformat(as_of_str) if as_of_str else date.today()
    except ValueError:
        as_of = date.today()

    # Today's meals (if any)
    today_menus = list(Menu.objects
                       .filter(date=as_of)
                       .select_related('recipe')
                       .prefetch_related('additional_recipes', 'freetext_components')
                       .order_by('meal_slot'))
    slot_order = ['cold_breakfast', 'hot_breakfast', 'lunch', 'dinner']
    slot_labels = dict(Menu.MEAL_SLOTS)
    today_by_slot = {m.meal_slot: m for m in today_menus}

    # Current-slot computation: which meal is "active right now"?
    # Approx windows: 6-10 cold, 10-12 hot, 12-17 lunch, 17-20 dinner, else none.
    now = datetime.now()
    hour = now.hour
    current_slot = None
    if as_of == date.today():
        if 6 <= hour < 10:
            current_slot = 'cold_breakfast'
        elif 10 <= hour < 12:
            current_slot = 'hot_breakfast'
        elif 12 <= hour < 17:
            current_slot = 'lunch'
        elif 17 <= hour < 21:
            current_slot = 'dinner'

    today_rows = []
    for s in slot_order:
        menu = today_by_slot.get(s)
        conflicts = _menu_conflict_set(menu) if menu else []
        today_rows.append({
            'slot': s,
            'label': slot_labels.get(s, s.title()),
            'menu': menu,
            'conflicts': conflicts,
            'is_current': s == current_slot,
        })

    # Next 6 days' highlights for the "coming up" strip
    upcoming_menus = (Menu.objects
                      .filter(date__gt=as_of, date__lte=as_of + timedelta(days=6))
                      .prefetch_related('additional_recipes')
                      .order_by('date', 'meal_slot'))
    from collections import defaultdict
    upcoming_by_date = defaultdict(list)
    for m in upcoming_menus:
        upcoming_by_date[m.date].append(m)
    upcoming_days = [
        {'date': d, 'label': d.strftime('%a %b %d'),
         'menus': sorted(upcoming_by_date[d], key=lambda m: slot_order.index(m.meal_slot) if m.meal_slot in slot_order else 99)}
        for d in sorted(upcoming_by_date.keys())
    ]

    census = Census.objects.filter(date=as_of).first()

    # Prep task summary for today
    prep_total = PrepTask.objects.filter(date=as_of).count()
    prep_done = PrepTask.objects.filter(date=as_of, completed=True).count()

    return render(request, 'myapp/display.html', {
        'as_of':         as_of,
        'is_today':      as_of == date.today(),
        'today_rows':    today_rows,
        'upcoming_days': upcoming_days,
        'census':        census,
        'has_any_today': bool(today_menus),
        'current_slot':  current_slot,
        'prep_total':    prep_total,
        'prep_done':     prep_done,
        'clock_time':    now.strftime('%-I:%M %p') if as_of == date.today() else None,
    })


def recipe_missing_quantities(request):
    """Focused work surface: lists recipes with the most null-quantity ingredients.
    Each row has a direct "Edit" link so Sean can knock them out in order."""
    from django.db.models import Count, Q

    # Per-recipe null-quantity count (excluding sub_recipe rows — those don't need qty)
    rows = (Recipe.objects
            .annotate(
                null_qty=Count('ingredients',
                               filter=Q(ingredients__quantity__isnull=True,
                                        ingredients__sub_recipe__isnull=True)),
                total_ings=Count('ingredients'),
            )
            .filter(null_qty__gt=0)
            .order_by('-null_qty', 'name'))

    total_null = sum(r.null_qty for r in rows)
    total_recipes_affected = rows.count()

    # For each recipe, list a few sample null-ingredient names for context
    enriched = []
    for r in rows[:80]:
        samples = list(r.ingredients.filter(quantity__isnull=True,
                                            sub_recipe__isnull=True)
                                    .values_list('name_raw', flat=True)[:5])
        enriched.append({
            'recipe': r,
            'null_qty': r.null_qty,
            'total_ings': r.total_ings,
            'samples': samples,
        })

    return render(request, 'myapp/recipe_missing_quantities.html', {
        'rows': enriched,
        'total_null': total_null,
        'total_recipes_affected': total_recipes_affected,
    })


def menu_bulk_link(request):
    """Walks each unlinked Menu row with fuzzy-matched recipe candidates.

    Rows are grouped logically by normalized dish_freetext so duplicates
    appear together; sort puts the most-frequent dish names first so the
    user's clicks have the biggest leverage (linking 'Chicken Parm' once
    may update 6 pending menus, not just 1).

    Actions per row:
      - Link this menu → single POST with menu_id+recipe_id
      - Link all N matching → POST with dish_freetext+recipe_id (batch)
      - Create new recipe  → routes through /recipe/new/ prefill + link_menu
      - Skip                → no-op"""
    from rapidfuzz import fuzz as _fuzz, process as _process
    from .management.commands.link_menus_to_recipes import _norm as _link_norm
    from collections import Counter

    if request.method == 'POST':
        recipe_id = request.POST.get('recipe_id')
        menu_id = request.POST.get('menu_id')
        batch_dish = (request.POST.get('batch_dish') or '').strip()
        try:
            r = Recipe.objects.get(pk=int(recipe_id)) if recipe_id else None
        except (Recipe.DoesNotExist, ValueError):
            messages.error(request, 'Invalid recipe.')
            return redirect(reverse('menu_bulk_link'))

        if r and batch_dish:
            # Batch: link every unlinked menu whose dish_freetext matches
            # (case-insensitive), excluding already-linked rows.
            qs = (Menu.objects
                  .filter(recipe__isnull=True)
                  .filter(dish_freetext__iexact=batch_dish))
            n = qs.update(recipe=r)
            messages.success(request,
                f'Linked {n} menu{"" if n == 1 else "s"} with "{batch_dish}" → {r.name}')
        elif r and menu_id:
            try:
                m = Menu.objects.get(pk=int(menu_id))
                m.recipe = r
                m.save(update_fields=['recipe'])
                messages.success(request, f'Linked "{m.dish_freetext}" → {r.name}')
            except (Menu.DoesNotExist, ValueError):
                messages.error(request, 'Invalid menu.')
        return redirect(reverse('menu_bulk_link'))

    # Pool of linkable recipes: composed_dish or meal, current, preloaded
    recipes = list(Recipe.objects.filter(
        level__in=('composed_dish', 'meal'), is_current=True
    ).order_by('name'))
    recipe_names_norm = {_link_norm(r.name): r for r in recipes}
    norm_keys = list(recipe_names_norm.keys())

    unlinked = list(Menu.objects
                    .filter(recipe__isnull=True)
                    .exclude(dish_freetext='')
                    .order_by('date', 'meal_slot'))

    # Count frequency per normalized dish_freetext for leverage ranking
    dish_key = lambda m: m.dish_freetext.lower().strip()
    freq = Counter(dish_key(m) for m in unlinked)

    rows = []
    for m in unlinked:
        candidates = []
        if norm_keys:
            matches = _process.extract(
                _link_norm(m.dish_freetext), norm_keys,
                scorer=_fuzz.token_set_ratio, limit=5,
            )
            for name_norm, score, idx in matches:
                if score >= 50:  # generous floor; user picks
                    candidates.append({
                        'recipe': recipe_names_norm[name_norm],
                        'score': int(score),
                    })
        rows.append({
            'menu': m,
            'candidates': candidates,
            'frequency': freq[dish_key(m)],
            'create_url': (reverse('recipe_new')
                           + f'?prefill_name={m.dish_freetext}&link_menu={m.id}'),
        })

    # Sort by frequency descending (leverage), then by date (chronology within group)
    rows.sort(key=lambda r: (-r['frequency'], r['menu'].date, r['menu'].meal_slot))

    return render(request, 'myapp/menu_bulk_link.html', {
        'rows': rows,
        'total_unlinked': len(rows),
        'total_recipes': len(recipes),
    })


def recipe_list(request):
    """Browse / search / categorized view of all recipes."""
    from collections import defaultdict
    q = (request.GET.get('q') or '').strip()
    qs = Recipe.objects.all().order_by('name').prefetch_related('ingredients')
    if q:
        qs = qs.filter(name__icontains=q)

    by_cat: dict[str, list[Recipe]] = defaultdict(list)
    for r in qs:
        r.is_composed = any(i.sub_recipe_id for i in r.ingredients.all())
        r.ingredient_count = r.ingredients.count()
        by_cat[_recipe_category(r)].append(r)

    # Put "My Meals" at top (user-created prefabs), then alphabetical
    categories = sorted(by_cat.keys(), key=lambda c: (0 if c == 'My Meals' else 1, c))
    groups = [(c, by_cat[c]) for c in categories]

    return render(request, 'myapp/recipe_list.html', {
        'groups': groups,
        'q':      q,
        'total':  Recipe.objects.count(),
        'match':  sum(len(v) for v in by_cat.values()),
    })


def recipe_detail(request, recipe_id: int):
    recipe = get_object_or_404(
        Recipe.objects.prefetch_related('ingredients__sub_recipe', 'ingredients__product', 'ingredients__yield_ref'),
        pk=recipe_id,
    )
    breakdown = recipe.estimated_cost_breakdown()
    # Version lineage (empty list if this is a solo v1)
    lineage = _lineage_recipes(recipe) if recipe.parent_recipe_id or recipe.versions.exists() else []
    return render(request, 'myapp/recipe_detail.html', {
        'recipe': recipe,
        'breakdown': breakdown,
        'lineage': lineage,
    })


@require_POST
def menu_save_prefab(request, menu_id: int):
    """Save the current menu's main + additional recipes as a reusable composed Recipe."""
    menu = get_object_or_404(Menu, pk=menu_id)
    name = (request.POST.get('prefab_name') or '').strip()
    if not name:
        return HttpResponseBadRequest("prefab_name required")

    linked: list[Recipe] = list(menu.additional_recipes.all())
    seen: set[int] = set()
    linked = [r for r in linked if not (r.id in seen or seen.add(r.id))]
    freetexts = list(menu.freetext_components.all())
    if len(linked) + len(freetexts) < 2:
        messages.error(request, "Need at least 2 components to save as a reusable meal.")
        return redirect(reverse('menu_detail', args=[menu.id]))

    if Recipe.objects.filter(name__iexact=name).exists():
        messages.error(request, f"A recipe named '{name}' already exists — pick a different name.")
        return redirect(reverse('menu_detail', args=[menu.id]))

    meal = Recipe.objects.create(
        name=name,
        level='meal',  # prefabs saved from a menu are always meal-level
        notes=f"Meal saved from menu {menu.date} {menu.get_meal_slot_display()}.",
    )
    for sub in linked:
        RecipeIngredient.objects.create(
            recipe=meal, sub_recipe=sub, name_raw=sub.name,
            quantity=Decimal('1'), unit='batch',
        )
    for fc in freetexts:
        RecipeIngredient.objects.create(
            recipe=meal, name_raw=fc.name[:300],
            quantity=fc.quantity, unit=fc.unit,
        )

    # Replace this menu's components with a single reference to the new prefab
    menu.additional_recipes.set([meal])
    menu.freetext_components.all().delete()
    menu.ingredients_raw = ''
    menu.save(update_fields=['ingredients_raw'])

    messages.success(
        request,
        f"Saved '{name}' as a meal and linked this slot to it — edit the recipe to update wherever it's used.",
    )
    return redirect(reverse('recipe_detail', args=[meal.id]))


def recipe_edit(request, recipe_id: int):
    recipe = get_object_or_404(Recipe, pk=recipe_id)
    if request.method == 'POST':
        form = RecipeForm(request.POST, instance=recipe)
        formset = RecipeIngredientFormSet(request.POST, instance=recipe)
        if form.is_valid() and formset.is_valid():
            form.save()
            formset.save()
            return redirect(reverse('recipe_detail', args=[recipe.id]))
    else:
        form = RecipeForm(instance=recipe)
        formset = RecipeIngredientFormSet(instance=recipe)
    # Shared datalist sources — rendered once in the template, referenced
    # by all yield_ref / sub_recipe inputs across the formset. Replaces
    # per-row <select> bloat (was ~500KB page; now ~30KB).
    yield_refs = list(YieldReference.objects.order_by('ingredient', 'prep_state'))
    sub_recipes = list(Recipe.objects.order_by('name').exclude(pk=recipe.pk))
    return render(request, 'myapp/recipe_form.html', {
        'recipe': recipe, 'form': form, 'formset': formset,
        'yield_refs': yield_refs,
        'sub_recipes': sub_recipes,
    })


def _lineage_recipes(recipe: Recipe) -> list[Recipe]:
    """Return all recipes in the same version lineage (same trunk), including the
    given recipe. Walks parent chain to root, then BFS-collects all descendants."""
    # Walk parent chain to root, with cycle guard
    root = recipe
    walk_seen = {root.pk}
    while root.parent_recipe_id and root.parent_recipe_id not in walk_seen:
        root = root.parent_recipe
        walk_seen.add(root.pk)
    # BFS all descendants from root (independent seen set so we actually collect them)
    seen = {root.pk}
    out = [root]
    frontier = [root]
    while frontier:
        next_frontier = []
        for r in frontier:
            for child in r.versions.all():
                if child.pk not in seen:
                    seen.add(child.pk)
                    out.append(child)
                    next_frontier.append(child)
        frontier = next_frontier
    return sorted(out, key=lambda r: r.version_number)


@require_POST
def recipe_new_version(request, recipe_id: int):
    """Create Recipe V{n+1} from an existing recipe. Copies fields + ingredients,
    points parent_recipe back, marks old as is_current=False."""
    current = get_object_or_404(Recipe, pk=recipe_id)

    lineage = _lineage_recipes(current)
    max_v = max(r.version_number for r in lineage)
    new_v = max_v + 1

    # Name: strip any trailing " V<digits>" then append new version
    import re as _re
    base_name = _re.sub(r'\s+V\d+\s*$', '', current.name).strip()
    new_name = f'{base_name} V{new_v}'

    # Safety: if somehow the new name collides, bump further
    while Recipe.objects.filter(name__iexact=new_name).exists():
        new_v += 1
        new_name = f'{base_name} V{new_v}'

    new = Recipe.objects.create(
        name=new_name,
        level=current.level,
        yield_servings=current.yield_servings,
        source_doc=current.source_doc,
        notes=current.notes,
        protein=current.protein,
        fat_health=current.fat_health,
        popularity='',  # fresh popularity tracking per version (per design memo)
        conflicts=list(current.conflicts or []),
        parent_recipe=current,
        version_number=new_v,
        is_current=True,
    )
    for ing in current.ingredients.all():
        RecipeIngredient.objects.create(
            recipe=new,
            name_raw=ing.name_raw,
            quantity=ing.quantity,
            unit=ing.unit,
            yield_pct=ing.yield_pct,
            yield_ref=ing.yield_ref,
            product=ing.product,
            sub_recipe=ing.sub_recipe,
        )
    # Flip all other lineage members to is_current=False; only `new` is current.
    Recipe.objects.filter(pk__in=[r.pk for r in lineage]).update(is_current=False)

    messages.success(request,
        f'Created {new_name} — {new.ingredients.count()} ingredients copied. '
        f'Edit below; previous version remains available in the history.')
    return redirect(reverse('recipe_edit', args=[new.id]))


def recipe_new(request):
    """Minimum-viable inline recipe creation. Fields: name, level, yield, notes.
    On save: redirect to recipe_edit so ingredients can be added.

    Supports ?prefill_name=... (used when linking from menu form) and
    ?link_menu=<id> (re-links that menu to the new recipe after save)."""
    from .models import LEVEL_CHOICES

    prefill_name = (request.GET.get('prefill_name') or '').strip()
    link_menu_id = request.GET.get('link_menu')

    if request.method == 'POST':
        name = (request.POST.get('name') or '').strip()
        level = request.POST.get('level') or 'meal'
        try:
            yield_servings = int(request.POST.get('yield_servings') or 40)
        except ValueError:
            yield_servings = 40
        notes = (request.POST.get('notes') or '').strip()
        post_link_menu_id = request.POST.get('link_menu') or link_menu_id

        errors = {}
        if not name:
            errors['name'] = 'Name is required.'
        elif Recipe.objects.filter(name__iexact=name).exists():
            errors['name'] = f'A recipe named "{name}" already exists — pick a different name.'
        if level not in dict(LEVEL_CHOICES):
            errors['level'] = 'Pick a valid level.'

        if errors:
            return render(request, 'myapp/recipe_new.html', {
                'prefill_name': name, 'level': level,
                'yield_servings': yield_servings, 'notes': notes,
                'link_menu_id': post_link_menu_id,
                'levels': LEVEL_CHOICES,
                'errors': errors,
            })

        recipe = Recipe.objects.create(
            name=name, level=level,
            yield_servings=yield_servings, notes=notes,
        )

        # Link back to a menu if requested
        if post_link_menu_id:
            try:
                menu = Menu.objects.get(pk=int(post_link_menu_id))
                menu.recipe = recipe
                menu.save(update_fields=['recipe'])
                messages.success(request, f'Created "{name}" and linked to {menu.date} {menu.get_meal_slot_display()}. Add ingredients below.')
            except (Menu.DoesNotExist, ValueError):
                messages.success(request, f'Created "{name}". Add ingredients below.')
        else:
            messages.success(request, f'Created "{name}". Add ingredients below.')

        return redirect(reverse('recipe_edit', args=[recipe.id]))

    # GET: render empty form
    # Pre-select level: if prefill_name looks like a meal-level name (has "with" or multi-word),
    # suggest 'meal'. Otherwise 'recipe'. Never silent — user confirms.
    suggested_level = 'meal' if (
        prefill_name and (' with ' in prefill_name.lower() or len(prefill_name.split()) >= 3)
    ) else 'recipe'

    return render(request, 'myapp/recipe_new.html', {
        'prefill_name': prefill_name,
        'level': suggested_level,
        'yield_servings': 40,
        'notes': '',
        'link_menu_id': link_menu_id,
        'levels': LEVEL_CHOICES,
        'errors': {},
    })


def yield_list(request):
    """Grouped per-section reference display. Each section gets a header +
    section-appropriate columns. Collapsed by default unless filtered."""
    q = (request.GET.get('q') or '').strip()
    section_filter = (request.GET.get('section') or '').strip()

    # Full queryset with filters
    qs = YieldReference.objects.all().order_by('section', 'ingredient', 'prep_state')
    if q:
        qs = qs.filter(models.Q(ingredient__icontains=q) | models.Q(prep_state__icontains=q))
    if section_filter:
        qs = qs.filter(section=section_filter)

    # Group by section
    from collections import defaultdict
    by_section: dict = defaultdict(list)
    for r in qs:
        by_section[r.section].append(r)

    section_labels = dict(YieldReference.SECTION_CHOICES)
    # Ordered section list for display
    section_order = [
        'meats', 'seafood', 'poultry',               # proteins first
        'vegetables', 'fruit', 'canned',             # produce
        'dairy',                                      # dairy
        'grains', 'pasta', 'dry_legumes', 'nuts_seeds',  # starches
        'flour', 'sweeteners', 'baking',             # baking
        'herbs_spices', 'fresh_herbs',                # seasonings
        'fats_oils', 'condiments', 'liquids', 'beverages',  # liquids
    ]

    # Row cap per section in the default view. Lifted when filter is active
    # (a search or explicit section selection).
    PER_SECTION_CAP = 40 if (q or section_filter) else 25

    sections = []
    for sect_key in section_order:
        rows = by_section.get(sect_key, [])
        if not rows:
            continue
        total = len(rows)
        shown_rows = rows[:PER_SECTION_CAP]
        sections.append({
            'key': sect_key,
            'label': section_labels.get(sect_key, sect_key.title()),
            'count': total,
            'shown': len(shown_rows),
            'rows': shown_rows,
            'render_as': _section_render_mode(sect_key),
            # Auto-expand only if filtered
            'expanded': bool(q or section_filter == sect_key),
            'has_more': total > len(shown_rows),
        })

    total_count = YieldReference.objects.count()
    all_sections_for_dropdown = sorted(
        [(k, section_labels.get(k, k), by_section.get(k, [])) for k in section_order if k in by_section],
        key=lambda t: t[1].lower()
    )

    return render(request, 'myapp/yield_list.html', {
        'sections': sections,
        'q': q,
        'selected_section': section_filter,
        'total_count': total_count,
        'filtered_count': qs.count(),
        'all_sections_for_dropdown': [
            {'key': k, 'label': label, 'count': len(rows)}
            for k, label, rows in all_sections_for_dropdown
        ],
    })


def _section_render_mode(section_key: str) -> str:
    """Map section to render-mode key used by the template.
    Different sections show different columns based on which data they carry."""
    if section_key in ('meats',):
        return 'meats'
    if section_key == 'seafood':
        return 'seafood'
    if section_key == 'poultry':
        return 'poultry'
    if section_key in ('herbs_spices', 'fresh_herbs'):
        return 'herbs'
    return 'standard'


def yield_edit(request, yield_id: int | None = None):
    instance = get_object_or_404(YieldReference, pk=yield_id) if yield_id else None
    if request.method == 'POST':
        form = YieldReferenceForm(request.POST, instance=instance)
        if form.is_valid():
            obj = form.save()
            messages.success(request, f"Saved: {obj}")
            return redirect(reverse('yield_list'))
    else:
        form = YieldReferenceForm(instance=instance)
    return render(request, 'myapp/yield_form.html', {'form': form, 'instance': instance})


@require_POST
def yield_delete(request, yield_id: int):
    obj = get_object_or_404(YieldReference, pk=yield_id)
    label = str(obj)
    obj.delete()
    messages.success(request, f"Deleted: {label}")
    return redirect(reverse('yield_list'))


# Tokens that — if ALL tokens fall in this set — mean the ingredient is pantry/100%-yield.
_YIELD_SKIP_WORDS = {
    'salt', 'pepper', 'sugar', 'water', 'flour', 'oil', 'milk', 'butter', 'egg', 'eggs',
    'baking', 'powder', 'soda', 'extract', 'vanilla', 'cumin', 'paprika', 'cayenne',
    'oregano', 'thyme', 'rosemary', 'chili', 'cinnamon', 'nutmeg', 'cream', 'buttermilk',
    'sour', 'yeast', 'cornstarch', 'vinegar', 'cocoa', 'honey', 'syrup', 'molasses',
    'mayo', 'mayonnaise', 'ketchup', 'mustard', 'worcestershire', 'soy', 'sesame',
    'broth', 'stock', 'ice', 'bacon', 'cheese', 'parmesan', 'ricotta', 'feta',
    'mozzarella', 'cheddar', 'wine', 'beer', 'juice', 'zest', 'brown', 'white',
    'ap', 'all-purpose',
}

# Tokens that — if ANY is present — mean the ingredient is a processed/bottled form
# (no meaningful trim yield to look up).
_YIELD_STRONG_SKIP_TOKENS = {
    'powder', 'sauce', 'oil', 'juice', 'flakes', 'flake', 'extract', 'zest', 'paste',
    'syrup', 'dried', 'canned', 'jarred', 'bottled', 'pickled', 'peppercorn',
    'peppercorns', 'anise', 'seasoning', 'seasonings', 'spice', 'spices',
    'frozen', 'seed', 'seeds', 'and',   # "salt and pepper" noise
}


def _is_pantry_skip(name_lc: str) -> bool:
    """True if the ingredient clearly has no meaningful BoY yield to look up."""
    tokens = {t.strip('., ()') for t in name_lc.replace(',', ' ').split()}
    if not tokens:
        return True
    if tokens.issubset(_YIELD_SKIP_WORDS):
        return True
    if tokens & _YIELD_STRONG_SKIP_TOKENS:
        return True
    return False


def _candidate_yield_refs(name_lc: str):
    """Return (up to) 8 candidate YieldReference rows for a name_raw, ranked by relevance."""
    tokens = [t.strip('., ()') for t in name_lc.replace(',', ' ').split()]
    tokens = [t for t in tokens if t and t not in _YIELD_SKIP_WORDS and len(t) > 2]
    if not tokens:
        return YieldReference.objects.none()
    q = models.Q()
    for t in tokens:
        q |= models.Q(ingredient__icontains=t)
    return YieldReference.objects.filter(q).order_by('ingredient', 'prep_state')[:12]


def _candidate_yield_refs_from_cache(name_lc: str, all_refs: list) -> list:
    """Python-side version of _candidate_yield_refs that uses a preloaded
    list of YieldReference instances. Avoids N+1 when called in a loop."""
    tokens = [t.strip('., ()') for t in name_lc.replace(',', ' ').split()]
    tokens = [t for t in tokens if t and t not in _YIELD_SKIP_WORDS and len(t) > 2]
    if not tokens:
        return []
    tokens_set = set(tokens)
    matches = [yr for yr in all_refs
               if any(t in (yr.ingredient or '').lower() for t in tokens_set)]
    matches.sort(key=lambda yr: ((yr.ingredient or '').lower(),
                                 (yr.prep_state or '').lower()))
    return matches[:12]


def yield_bridge(request):
    """Bulk-link RecipeIngredient.yield_ref for ingredients with BoY candidates."""
    if request.method == 'POST':
        linked = 0
        for key, val in request.POST.items():
            if not key.startswith('ref_for_'):
                continue
            if not val:
                continue
            name_lc = key[len('ref_for_'):]
            try:
                ref = YieldReference.objects.get(pk=int(val))
            except (YieldReference.DoesNotExist, ValueError):
                continue
            count = RecipeIngredient.objects.annotate(
                name_lc=models.functions.Lower('name_raw')
            ).filter(name_lc=name_lc, yield_ref__isnull=True).update(yield_ref=ref)
            linked += count
        messages.success(request, f"Linked {linked} RecipeIngredient rows to yield references.")
        return redirect(reverse('yield_bridge'))

    # GET: build list of unlinked, non-pantry ingredients with candidates
    from django.db.models.functions import Lower
    unlinked = (
        RecipeIngredient.objects
        .filter(yield_ref__isnull=True, sub_recipe__isnull=True)
        .annotate(name_lc=Lower('name_raw'))
        .values('name_lc')
        .annotate(n=models.Count('id'))
        .order_by('-n')
    )

    # Preload all YieldReferences once so candidate lookup is Python-side
    # (was 1 query per unique name_lc — up to 56 queries for 40-row page).
    all_refs = list(YieldReference.objects.all())
    rows = []
    for row in unlinked:
        name_lc = row['name_lc']
        if _is_pantry_skip(name_lc):
            continue
        candidates = _candidate_yield_refs_from_cache(name_lc, all_refs)
        if not candidates:
            continue
        rows.append({
            'name_lc': name_lc,
            'count': row['n'],
            'candidates': candidates,
        })
        if len(rows) >= 40:
            break

    return render(request, 'myapp/yield_bridge.html', {'rows': rows})



# ---- Leftover aging / fridge-rotation view ----

def leftovers_view(request):
    """List active leftovers (service logged with leftover_qty > 0, no disposal
    logged yet). Color-coded by age. Supports inline log-disposal + "consumed"
    (marks leftover as 0 discarded = fully eaten)."""
    from .models import MealService
    from datetime import timedelta
    from decimal import Decimal

    today = date.today()
    active = (MealService.objects
              .filter(post_service_leftover_qty__gt=0, discarded_qty__isnull=True)
              .select_related('menu', 'menu__recipe')
              .order_by('menu__date'))

    rows = []
    for svc in active:
        age_days = (today - svc.menu.date).days
        if age_days < 0:
            continue
        if age_days >= 5:
            urgency = 'red'
            banner = 'DAY 5 — check now'
        elif age_days >= 3:
            urgency = 'amber'
            banner = f'Day {age_days} — check soon'
        else:
            urgency = 'green'
            banner = f'Day {age_days} — fresh'
        rows.append({
            'service': svc,
            'age_days': age_days,
            'urgency': urgency,
            'banner': banner,
        })

    # Sort: most urgent (highest age) first
    rows.sort(key=lambda r: -r['age_days'])

    # Counts for summary
    red_count = sum(1 for r in rows if r['urgency'] == 'red')
    amber_count = sum(1 for r in rows if r['urgency'] == 'amber')
    green_count = sum(1 for r in rows if r['urgency'] == 'green')

    # Distinguish "caught up" (has logged services, all handled) from
    # "never logged" (no MealService records exist) — different empty states.
    ever_logged = MealService.objects.exists()

    return render(request, 'myapp/leftovers.html', {
        'today': today,
        'rows': rows,
        'red_count': red_count,
        'amber_count': amber_count,
        'green_count': green_count,
        'total': len(rows),
        'ever_logged': ever_logged,
    })


@require_POST
def mealservice_mark_consumed(request, service_id: int):
    """Mark a leftover as fully consumed (discarded_qty=0). Different from
    log-disposal which records actual thrown-out quantity."""
    from .models import MealService
    from datetime import datetime
    from decimal import Decimal
    svc = get_object_or_404(MealService, pk=service_id)
    svc.discarded_qty = Decimal('0')
    svc.discarded_at = datetime.now()
    svc.save(update_fields=['discarded_qty', 'discarded_at', 'updated_at'])
    messages.success(request,
        f"Marked leftover for {svc.menu.recipe.name if svc.menu.recipe else svc.menu.dish_freetext} as fully consumed.")
    return redirect(reverse('leftovers_view'))


# ---- Popularity analytics (current MealService data) ----

def popularity_dashboard(request):
    """Aggregate MealService records by recipe. Shows current-era popularity
    data — distinct from /historical/ which shows the Jan 2026 Production
    Tracker parse. Populates as Sean logs service records via /menu/<id>/."""
    from .models import MealService
    from decimal import Decimal
    from collections import defaultdict

    # Group services by recipe (via menu.recipe or menu.additional_recipes)
    by_recipe: dict = defaultdict(list)
    unlinked: list = []
    for svc in (MealService.objects
                .select_related('menu', 'menu__recipe')
                .prefetch_related('menu__additional_recipes')):
        recipes = list(svc.menu.additional_recipes.all())
        if svc.menu.recipe_id and svc.menu.recipe not in recipes:
            recipes.append(svc.menu.recipe)
        if not recipes:
            unlinked.append(svc)
            continue
        for r in recipes:
            by_recipe[r.id].append(svc)

    # Build per-recipe rollups
    rows = []
    for recipe_id, services in by_recipe.items():
        from .models import Recipe
        recipe = Recipe.objects.filter(pk=recipe_id).first()
        if not recipe:
            continue
        # Only include services with numeric prep_qty for averaging
        prepped_vals = [s.prepped_qty for s in services if s.prepped_qty is not None]
        if not prepped_vals:
            continue

        eat_rates = [s.immediate_eat_rate for s in services if s.immediate_eat_rate is not None]
        consume_rates = [s.total_consumption_rate for s in services if s.total_consumption_rate is not None]

        avg_eat = sum(eat_rates) / len(eat_rates) if eat_rates else None
        avg_consume = sum(consume_rates) / len(consume_rates) if consume_rates else None

        rows.append({
            'recipe': recipe,
            'n_services': len(services),
            'avg_prepped': sum(prepped_vals) / len(prepped_vals),
            'avg_eat_rate': avg_eat,
            'avg_consumption_rate': avg_consume,
            'services': services[:5],  # sample for drill-down hint
            'has_disposal_data': any(s.discarded_qty is not None for s in services),
        })

    # Rank
    with_consumption = [r for r in rows if r['avg_consumption_rate'] is not None]
    top_popular = sorted(with_consumption, key=lambda r: -r['avg_consumption_rate'])[:10]
    bottom_popular = sorted(with_consumption, key=lambda r: r['avg_consumption_rate'])[:10]

    total_services = sum(r['n_services'] for r in rows)

    return render(request, 'myapp/popularity.html', {
        'rows_by_name': sorted(rows, key=lambda r: r['recipe'].name.lower()),
        'top_popular': top_popular,
        'bottom_popular': bottom_popular,
        'total_services': total_services,
        'total_dishes': len(rows),
        'unlinked_services': len(unlinked),
    })


# ---- Historical dish performance (Production Tracker) ----

def historical_dishes(request):
    """Display Jan 2026 Production Tracker data — historical waste ratios
    and consumption rates. Demo prop for the 'we have real data' narrative.

    JSON source: .historical_stats/production_tracker.json, produced by the
    import_production_tracker management command. If the file is missing,
    the view shows an empty-state prompt."""
    import json
    from pathlib import Path
    from django.conf import settings as _settings

    json_path = _settings.BASE_DIR / '.historical_stats' / 'production_tracker.json'
    if not json_path.exists():
        return render(request, 'myapp/historical.html', {'stats': None})

    try:
        data = json.loads(json_path.read_text())
    except (json.JSONDecodeError, OSError):
        return render(request, 'myapp/historical.html', {'stats': None})

    dishes = data.get('dishes', [])
    # Sort buckets
    multi_service = [d for d in dishes if d['n_services'] >= 2]
    single_service = [d for d in dishes if d['n_services'] == 1]

    top_waste = sorted(multi_service, key=lambda d: -d['avg_waste_ratio'])[:10]
    top_consumed = sorted(multi_service, key=lambda d: d['avg_waste_ratio'])[:10]
    interesting_singles = sorted(single_service, key=lambda d: -d['avg_waste_ratio'])[:6]

    return render(request, 'myapp/historical.html', {
        'stats': {
            'generated_at': data.get('generated_at', ''),
            'total_services': data.get('total_services', 0),
            'unique_dishes': data.get('unique_dishes', 0),
            'top_waste': top_waste,
            'top_consumed': top_consumed,
            'interesting_singles': interesting_singles,
            'total_multi_service': len(multi_service),
        },
    })


# ---- Demo readiness page ----

def demo_ready(request):
    """Morning-of demo checklist. Groups status by demo scene + data quality +
    external blockers. Each row is one actionable status with a fix link."""
    from datetime import timedelta
    from django.db.models import Count, Q
    from pathlib import Path
    import os

    today = date.today()

    checks: list[dict] = []

    def add(section: str, status: str, label: str, detail: str = '', link: str = '', link_label: str = ''):
        checks.append({
            'section': section,
            'status': status,  # 'green' | 'yellow' | 'red'
            'label': label,
            'detail': detail,
            'link': link,
            'link_label': link_label,
        })

    # --- Scene 1: Ambient / wall display ---
    today_menus = Menu.objects.filter(date=today).count()
    if today_menus >= 3:
        add('Scene 1 — Ambient (wall display)', 'green',
            f"Today's menu populated ({today_menus} slots)",
            '/display/ will have dishes to show',
            reverse('kitchen_display'), 'open display')
    elif today_menus:
        add('Scene 1 — Ambient (wall display)', 'yellow',
            f"Today's menu partial ({today_menus} slots)",
            'Display will show some dishes; others blank',
            reverse('kitchen_display'), 'open display')
    else:
        add('Scene 1 — Ambient (wall display)', 'red',
            "Today has no menu",
            "Wall display will show 'No menu scheduled'. Use ?as_of= to preview a populated day.",
            reverse('kitchen_display') + f'?as_of=2026-04-27', 'preview 4/27')

    # --- Scene 2: Albert authors a cell ---
    # Need: empty cells to demo against, and the "+ New Recipe" + order-guide paths to look good.
    upcoming_end = today + timedelta(days=14)
    upcoming_total = Menu.objects.filter(date__gte=today, date__lte=upcoming_end).count()
    upcoming_linked = Menu.objects.filter(
        date__gte=today, date__lte=upcoming_end, recipe__isnull=False
    ).count()
    linked_pct = int(100 * upcoming_linked / upcoming_total) if upcoming_total else 0
    if linked_pct >= 50:
        add('Scene 2 — Albert authors', 'green',
            f"Next 14 days: {upcoming_linked}/{upcoming_total} menus linked ({linked_pct}%)",
            'Cost badges will be visible throughout the demo calendar',
            reverse('calendar_current'), 'open calendar')
    elif linked_pct >= 20:
        add('Scene 2 — Albert authors', 'yellow',
            f"Next 14 days: {upcoming_linked}/{upcoming_total} menus linked ({linked_pct}%)",
            'Some cells will show cost badges; most will be freetext-only',
            reverse('menu_bulk_link'), 'run bulk-link')
    else:
        add('Scene 2 — Albert authors', 'red',
            f"Next 14 days: {upcoming_linked}/{upcoming_total} menus linked ({linked_pct}%)",
            'Most calendar cells will have no cost badges — the story is thin. Bulk-link is the fix.',
            reverse('menu_bulk_link'), 'run bulk-link')

    # Cost coverage for linked menus — approximate via a Python-side
    # "priceable" count on prefetched ingredients (has product + qty + unit).
    # Avoids calling recipe.estimated_cost_breakdown(), which issues one
    # invoice-lookup query per ingredient (was ~30+ queries for this block).
    linked_menus = list(Menu.objects
                        .filter(date__gte=today, date__lte=upcoming_end,
                                recipe__isnull=False)
                        .select_related('recipe')
                        .prefetch_related('recipe__ingredients'))
    total_linked = len(linked_menus)
    priced = 0
    for m in linked_menus:
        ings = list(m.recipe.ingredients.all())
        if not ings:
            continue
        priceable = sum(1 for i in ings
                        if i.product_id and i.quantity and i.unit)
        if priceable / len(ings) >= 0.5:
            priced += 1
    if total_linked and priced / total_linked >= 0.7:
        add('Scene 2 — Albert authors', 'green',
            f"Linked menus with ≥50% priceable ingredients: {priced}/{total_linked}",
            'Cost badges will show real numbers')
    elif total_linked:
        add('Scene 2 — Albert authors', 'yellow',
            f"Linked menus with ≥50% priceable ingredients: {priced}/{total_linked}",
            'Many linked menus have null ingredient quantities. Fix to light up badges.',
            reverse('recipe_missing_quantities'), 'fill quantities')

    # --- Scene 3: Management / COGs ---
    cache_dir = Path('.invoice_totals')
    current_month_cache = cache_dir / f'{today.year:04d}-{today.month:02d}.json'
    if current_month_cache.exists():
        import json
        entries = json.loads(current_month_cache.read_text())
        total = sum(e.get('total', 0) for e in entries)
        if total > 5000:
            add('Scene 3 — Management (COGs)', 'green',
                f"{len(entries)} invoices cached, ${total:,.2f}",
                'COGs dashboard will have substance')
        elif total > 0:
            add('Scene 3 — Management (COGs)', 'yellow',
                f"{len(entries)} invoices cached, only ${total:,.2f}",
                'COGs shows near-empty April. Run invoice batch or verify Drive inbox.',
                reverse('cogs_dashboard'), 'open cogs')
        else:
            add('Scene 3 — Management (COGs)', 'red',
                "No April invoices cached",
                'COGs will show $0 spent. Run invoice batch.',
                reverse('cogs_dashboard'), 'open cogs')
    else:
        add('Scene 3 — Management (COGs)', 'red',
            "No invoice-totals cache file for current month",
            f"Expected: .invoice_totals/{current_month_cache.name}")

    # Dietary tags visible
    tagged = Recipe.objects.exclude(conflicts=[]).count()
    total_recipes = Recipe.objects.count()
    if tagged >= total_recipes * 0.5:
        add('Scene 3 — Management (dietary story)', 'green',
            f"{tagged}/{total_recipes} recipes have dietary conflicts tagged",
            'Management scene can show "here\'s what flags for a gluten allergy"')
    else:
        add('Scene 3 — Management (dietary story)', 'yellow',
            f"Only {tagged}/{total_recipes} recipes tagged",
            'Run auto_tag_conflicts or tag manually via /recipes/.')

    # --- Scene 4: IT ask ---
    it_doc = Path('docs/it-access-request.md')
    if it_doc.exists():
        size = it_doc.stat().st_size
        add('Scene 4 — IT ask handout', 'green',
            f"docs/it-access-request.md present ({size} bytes)",
            'Print or email to IT admin before the meeting')
    else:
        add('Scene 4 — IT ask handout', 'red',
            "docs/it-access-request.md missing",
            'The 1-pager isn\'t in the repo. Check git log.')

    # --- Data quality ---
    null_qty = RecipeIngredient.objects.filter(quantity__isnull=True, sub_recipe__isnull=True).count()
    if null_qty == 0:
        add('Data quality', 'green', 'All recipe ingredients have quantities')
    elif null_qty < 50:
        add('Data quality', 'yellow',
            f'{null_qty} RecipeIngredients have null quantity',
            'Blocks cost calc on those ingredients',
            reverse('recipe_missing_quantities'), 'fill quantities')
    else:
        add('Data quality', 'red',
            f'{null_qty} RecipeIngredients have null quantity',
            'Biggest single cost-coverage unlock',
            reverse('recipe_missing_quantities'), 'fill quantities')

    proteinless = Recipe.objects.filter(protein='').count()
    if proteinless == 0:
        add('Data quality', 'green', 'All recipes have a protein label')
    elif proteinless < 20:
        add('Data quality', 'yellow',
            f'{proteinless} recipes have no protein label',
            'Dish-suggestion protein-rotation rules silently skip these')
    else:
        add('Data quality', 'red',
            f'{proteinless} recipes have no protein label',
            'Run auto_tag_protein or set manually')

    # --- Pipeline health ---
    logs_dir = Path('logs')
    recent_logs = []
    if logs_dir.exists():
        recent_logs = sorted(logs_dir.glob('invoice_batch_*.log'))[-5:]
    if recent_logs:
        any_error = False
        for log in recent_logs:
            try:
                content = log.read_text()
                if 'Traceback' in content or 'ERROR' in content:
                    any_error = True
                    break
            except OSError:
                pass
        if not any_error:
            add('Pipeline health', 'green',
                f'Last 5 invoice batch runs clean (through {recent_logs[-1].name})')
        else:
            add('Pipeline health', 'red',
                'Error found in recent batch logs',
                f'Check {recent_logs[-1].name}')
    else:
        add('Pipeline health', 'yellow',
            'No recent invoice batch logs',
            'Pipeline may be idle or stopped')

    recent_alerts = InvoiceLineItem.objects.filter(
        price_flagged=True, invoice_date__gte=today - timedelta(days=7),
    ).count()
    add('Pipeline health', 'green' if recent_alerts else 'yellow',
        f'{recent_alerts} price alerts in last 7 days',
        'Fresh anomalies = demo has "we catch things" story' if recent_alerts else 'No recent alerts — weak "we watch prices" demo beat',
        reverse('price_alerts'), 'open alerts')

    # --- External blockers ---
    april_census = Census.objects.filter(date__year=today.year, date__month=today.month).count()
    if april_census >= 15:
        add('External blockers', 'green',
            f'Current-month census populated ({april_census} days)')
    elif april_census > 0:
        add('External blockers', 'yellow',
            f'Current-month census partial ({april_census} days)',
            'Program director still updating — chase via Google Tasks')
    else:
        add('External blockers', 'red',
            'Current-month census empty',
            'Blocks headcount scaling throughout. Chase program director.')

    add('External blockers', 'red',
        'Kitchen display hardware not deployed',
        'Apolosign ordered? Mount? Network? The wall display is the ambient-anchor for scene 1.')

    add('External blockers', 'yellow',
        'Rehearsal: not tracked',
        'Memory: "rehearse at least twice before demo day, Albert once." Both uncertain here.')

    # --- Organize by section ---
    from collections import defaultdict
    by_section: dict = defaultdict(list)
    for c in checks:
        by_section[c['section']].append(c)
    section_order = [
        'Scene 1 — Ambient (wall display)',
        'Scene 2 — Albert authors',
        'Scene 3 — Management (COGs)',
        'Scene 3 — Management (dietary story)',
        'Scene 4 — IT ask handout',
        'Data quality',
        'Pipeline health',
        'External blockers',
    ]
    sections = [(s, by_section[s]) for s in section_order if s in by_section]

    # Rollup: count by status
    red = sum(1 for c in checks if c['status'] == 'red')
    yellow = sum(1 for c in checks if c['status'] == 'yellow')
    green = sum(1 for c in checks if c['status'] == 'green')

    return render(request, 'myapp/demo_ready.html', {
        'today': today,
        'sections': sections,
        'red_count': red,
        'yellow_count': yellow,
        'green_count': green,
    })


# ---- Price creep alerts ----

def price_alerts(request):
    """Classify price-flagged InvoiceLineItem rows into 4 buckets so each
    failure mode gets its own fix path:

      1. mapping_error — canonical name shares zero meaningful tokens with
         raw_description. The alert is a symptom of a bad ProductMapping,
         not a real price change. Fix: unmap + remap via bridge review.

      2. extended_leak — current price >> historical AND absolute is high.
         Suggests unit_price field captured an extended_amount (line total)
         instead of per-unit. Fix: parser / db_write investigation.

      3. unit_drift — ratio >2x or <0.5x AND either the case_size has
         multiple distinct values in history OR one side of the ratio is
         a small per-unit number (<$5). Suggests $/lb vs $/case comparison.
         Fix: unit normalization or per-product case_size standardization.

      4. real_change — none of the above; legitimate price movement worth
         reviewing for sourcing / budget.

    The underlying price_flagged field is set at write time in
    invoice_processor/db_write.py; this view just classifies what's flagged."""
    from django.db.models import Avg, Count, Max
    from datetime import timedelta
    from collections import defaultdict
    import re

    today = date.today()
    window_start = today - timedelta(days=30)
    history_start = today - timedelta(days=90)

    # Sysco brand prefix tokens — treat as noise, not product identity
    _NOISE = {'whlfcls', 'grecosn', 'coopr', 'emba', 'ssa', 'sys', 'cls',
              'imp', 'cur', 'ckd', 'bnls', 'sysfpnat', 'syfpnat', 'sysclb'}

    def _tokens(s):
        return {t.lower() for t in re.findall(r'[A-Za-z]{3,}', s or '')} - _NOISE

    # Recent flags — group by (product, vendor), keep most-recent per group
    recent = (InvoiceLineItem.objects
              .filter(price_flagged=True,
                      invoice_date__gte=window_start,
                      product__isnull=False,
                      unit_price__isnull=False)
              .select_related('product', 'vendor')
              .order_by('-invoice_date'))

    grouped: dict = {}  # (product_id, vendor_id) → line
    for ili in recent:
        key = (ili.product_id, ili.vendor_id)
        if key not in grouped:
            grouped[key] = ili

    buckets: dict = {
        'mapping_error': [],
        'extended_leak': [],
        'unit_drift': [],
        'real_change': [],
    }

    # Bulk-compute historical avg price + distinct case_size count for every
    # (product, vendor) group in one pass each. Was 35x+19x N+1 queries.
    pv_keys = list(grouped.keys())
    pids = {k[0] for k in pv_keys}
    vids = {k[1] for k in pv_keys}

    avg_map: dict = {}
    if pv_keys:
        # Historical average over the 90-day window for these (product, vendor) pairs
        avg_rows = (InvoiceLineItem.objects
                    .filter(product_id__in=pids, vendor_id__in=vids,
                            invoice_date__gte=history_start,
                            unit_price__isnull=False,
                            unit_price__gt=0)
                    .values('product_id', 'vendor_id')
                    .annotate(avg=Avg('unit_price'),
                              max_date=Max('invoice_date')))
        for r in avg_rows:
            avg_map[(r['product_id'], r['vendor_id'])] = r['avg']

    # Distinct case_size count per (product, vendor)
    size_map: dict = {}
    if pv_keys:
        size_rows = (InvoiceLineItem.objects
                     .filter(product_id__in=pids, vendor_id__in=vids)
                     .exclude(case_size='')
                     .values('product_id', 'vendor_id', 'case_size')
                     .distinct())
        for r in size_rows:
            k = (r['product_id'], r['vendor_id'])
            size_map[k] = size_map.get(k, 0) + 1

    for (pid, vid), ili in grouped.items():
        # Historical avg excluding the flagged line itself — approximate by
        # using the bulk-computed mean; the earlier per-query version excluded
        # ili.invoice_date from the window, but the classification is coarse
        # enough that including/excluding one datapoint rarely flips the bucket.
        avg_price = avg_map.get((pid, vid))
        if avg_price is None or avg_price == 0:
            continue
        avg = Decimal(avg_price)
        cur = Decimal(ili.unit_price)
        ratio = cur / avg
        delta_pct = (ratio - 1) * 100

        # Classification
        canon_tokens = _tokens(ili.product.canonical_name)
        desc_tokens = _tokens(ili.raw_description)
        overlap = canon_tokens & desc_tokens

        if canon_tokens and desc_tokens and len(canon_tokens) >= 1 and len(desc_tokens) >= 2 and not overlap:
            bucket = 'mapping_error'
        elif ratio > Decimal('5') and cur > Decimal('50'):
            bucket = 'extended_leak'
        elif ratio > Decimal('2') or ratio < Decimal('0.5'):
            distinct_sizes = size_map.get((pid, vid), 0)
            if distinct_sizes > 1 or cur < Decimal('5') or avg < Decimal('5'):
                bucket = 'unit_drift'
            else:
                bucket = 'real_change'
        else:
            bucket = 'real_change'

        buckets[bucket].append({
            'ili': ili,
            'avg_price': avg.quantize(Decimal('0.01')),
            'delta_pct': delta_pct.quantize(Decimal('0.1')),
            'direction': 'up' if delta_pct > 0 else 'down',
            'ratio': ratio.quantize(Decimal('0.01')),
        })

    # Sort each bucket by magnitude descending
    for b in buckets.values():
        b.sort(key=lambda e: abs(e['delta_pct']), reverse=True)

    total = sum(len(b) for b in buckets.values())

    # Vendor breakdown of flagged counts in window (unchanged)
    vendor_counts = (InvoiceLineItem.objects
                     .filter(price_flagged=True, invoice_date__gte=window_start)
                     .values('vendor__name')
                     .annotate(n=Count('id'))
                     .order_by('-n'))

    # Total flag volume over time (last 4 months)
    from calendar import monthrange
    trend = []
    for delta in range(3, -1, -1):
        y, m = today.year, today.month - delta
        while m <= 0:
            y -= 1
            m += 12
        start = date(y, m, 1)
        end = date(y, m, monthrange(y, m)[1])
        n = InvoiceLineItem.objects.filter(
            price_flagged=True, invoice_date__gte=start, invoice_date__lte=end,
        ).count()
        trend.append({'label': start.strftime('%b %Y'), 'n': n})
    trend_max = max((t['n'] for t in trend), default=1) or 1

    return render(request, 'myapp/price_alerts.html', {
        'today': today,
        'window_days': 30,
        'buckets': buckets,
        'total_flagged_window': total,
        'vendor_counts': list(vendor_counts),
        'trend': trend,
        'trend_max': trend_max,
    })


# ---- Pipeline health dashboard ----

def pipeline_health(request):
    """Operational dashboard for the invoice pipeline — mapping rate,
    match-confidence distribution, per-vendor fidelity, cache sizes,
    recent batch log status. Central place to answer 'is the pipeline
    actually healthy' in one glance.

    Cheap to compute — no DocAI calls, just DB aggregates + filesystem
    stats. Safe to hit frequently."""
    from collections import Counter
    from pathlib import Path
    from django.db.models import Count
    from datetime import timedelta
    import os

    today = date.today()
    window_start = today - timedelta(days=30)

    # --- DB-side coverage ---
    total_ili = InvoiceLineItem.objects.count()
    mapped = InvoiceLineItem.objects.filter(product__isnull=False).count()
    with_price = InvoiceLineItem.objects.filter(unit_price__isnull=False).count()
    flagged = InvoiceLineItem.objects.filter(
        price_flagged=True, invoice_date__gte=window_start,
    ).count()
    unmatched = InvoiceLineItem.objects.filter(match_confidence='unmatched').count()

    # --- Match confidence histogram ---
    conf_rows = (InvoiceLineItem.objects
                 .values('match_confidence')
                 .annotate(n=Count('id'))
                 .order_by('-n'))
    confidence_rows = [
        {'label': r['match_confidence'] or '(blank)', 'n': r['n']}
        for r in conf_rows
    ]

    # --- Per-vendor fidelity ---
    vendor_rows = []
    for v in Vendor.objects.all().order_by('name'):
        vt = InvoiceLineItem.objects.filter(vendor=v).count()
        vm = InvoiceLineItem.objects.filter(vendor=v, product__isnull=False).count()
        if vt:
            vendor_rows.append({
                'name': v.name, 'total': vt, 'mapped': vm,
                'pct': round(vm / vt * 100, 1),
            })
    vendor_rows.sort(key=lambda r: -r['total'])

    # --- Cache + artifact sizes ---
    from django.conf import settings as _settings
    base = _settings.BASE_DIR
    def _dir_stats(p):
        if not p.exists():
            return {'exists': False, 'files': 0, 'size_mb': 0}
        files = list(p.iterdir())
        size = sum(f.stat().st_size for f in files if f.is_file())
        return {'exists': True, 'files': len(files), 'size_mb': round(size / 1024 / 1024, 2)}

    ocr_cache = _dir_stats(base / '.ocr_cache')
    invoice_totals = _dir_stats(base / '.invoice_totals')
    historical_stats = _dir_stats(base / '.historical_stats')

    # --- Recent batch log health ---
    logs_dir = base / 'logs'
    recent_logs = sorted(logs_dir.glob('invoice_batch_*.log'))[-10:] if logs_dir.exists() else []
    log_health = {'recent': len(recent_logs), 'errors_found': 0, 'last': None}
    for log in recent_logs:
        try:
            content = log.read_text()
            if 'Traceback' in content or 'ERROR' in content:
                log_health['errors_found'] += 1
            log_health['last'] = log.name
        except OSError:
            pass

    # --- Orphan products + unmapped age ---
    from django.db.models import Count as _Count
    orphan_count = Product.objects.annotate(n=_Count('invoicelineitem')).filter(n=0).count()

    # --- Unmapped queue — how long have unmapped items been sitting ---
    oldest_unmapped = (InvoiceLineItem.objects
                       .filter(match_confidence='unmatched')
                       .order_by('invoice_date')
                       .first())
    unmapped_age_days = None
    if oldest_unmapped and oldest_unmapped.invoice_date:
        unmapped_age_days = (today - oldest_unmapped.invoice_date).days

    # --- Recipe linkage ---
    menu_total = Menu.objects.count()
    menu_linked = Menu.objects.filter(recipe__isnull=False).count()

    ri_total = RecipeIngredient.objects.count()
    ri_with_product = RecipeIngredient.objects.filter(product__isnull=False).count()
    ri_with_qty = RecipeIngredient.objects.filter(quantity__isnull=False).count()
    ri_with_yieldref = RecipeIngredient.objects.filter(yield_ref__isnull=False).count()

    return render(request, 'myapp/pipeline_health.html', {
        'today': today,
        'total_ili': total_ili,
        'mapped': mapped,
        'mapped_pct': round(mapped / total_ili * 100, 1) if total_ili else 0,
        'with_price': with_price,
        'with_price_pct': round(with_price / total_ili * 100, 1) if total_ili else 0,
        'unmatched': unmatched,
        'flagged_30d': flagged,
        'confidence_rows': confidence_rows,
        'vendor_rows': vendor_rows,
        'ocr_cache': ocr_cache,
        'invoice_totals': invoice_totals,
        'historical_stats': historical_stats,
        'log_health': log_health,
        'orphan_count': orphan_count,
        'unmapped_age_days': unmapped_age_days,
        'menu_total': menu_total,
        'menu_linked': menu_linked,
        'menu_linked_pct': round(menu_linked / menu_total * 100, 1) if menu_total else 0,
        'ri_total': ri_total,
        'ri_with_product': ri_with_product,
        'ri_with_product_pct': round(ri_with_product / ri_total * 100, 1) if ri_total else 0,
        'ri_with_qty': ri_with_qty,
        'ri_with_qty_pct': round(ri_with_qty / ri_total * 100, 1) if ri_total else 0,
        'ri_with_yieldref': ri_with_yieldref,
        'ri_with_yieldref_pct': round(ri_with_yieldref / ri_total * 100, 1) if ri_total else 0,
    })


# ---- Mapping Review queue (Phase 2B — Django UI replacing Sheets workflow) ----

def mapping_review(request):
    """List view of pending ProductMappingProposal rows.

    Replaces the Sheets-based Mapping Review tab. Each pending row shows:
    vendor + raw_description + suggested canonical + score, with three
    actions per row:
      - Approve (uses suggested canonical, or override via dropdown)
      - Reject (suppresses re-suggestion of same vendor+desc)
      - Skip (leaves pending; review later)

    Filters: vendor, status, source. Sort: score desc, recency desc.

    Cheap to render — paginated, no fancy joins. The approve action
    handles the FK backfill to all matching ILI rows transactionally."""
    from .models import ProductMappingProposal
    from .taxonomy import infer_taxonomy
    from django.db.models import Count

    status_filter = request.GET.get('status', 'pending')
    vendor_filter = request.GET.get('vendor', '')
    sort_by = request.GET.get('sort', 'frequency')   # 'frequency' | 'recent'

    qs = (ProductMappingProposal.objects
          .select_related('vendor', 'suggested_product', 'reviewed_by'))

    if status_filter and status_filter != 'all':
        qs = qs.filter(status=status_filter)
    if vendor_filter:
        qs = qs.filter(vendor__name=vendor_filter)

    # Pre-compute ILI counts for ALL (vendor, raw_description) pairs in one
    # aggregate query — avoids a per-proposal SELECT COUNT in the loop.
    ili_counts_qs = (InvoiceLineItem.objects
                     .values('vendor_id', 'raw_description')
                     .annotate(n=Count('id')))
    ili_count_map = {(r['vendor_id'], r['raw_description']): r['n']
                     for r in ili_counts_qs}

    # Pre-compute most-recent section_hint per (vendor, raw_description) for
    # use in taxonomy inference of the inline-create form.
    ili_hint_qs = (InvoiceLineItem.objects
                   .filter(section_hint__gt='')
                   .values('vendor_id', 'raw_description', 'section_hint')
                   .order_by('-invoice_date'))
    section_hint_map = {}
    for r in ili_hint_qs:
        key = (r['vendor_id'], r['raw_description'])
        if key not in section_hint_map:
            section_hint_map[key] = r['section_hint']

    proposals = list(qs)
    proposal_rows = []
    for p in proposals:
        n = ili_count_map.get((p.vendor_id, p.raw_description), 0)
        # Pre-compute taxonomy inference for the inline-create form
        section_hint = section_hint_map.get((p.vendor_id, p.raw_description))
        subset = p.suggested_product.canonical_name if p.suggested_product else None
        inferred = infer_taxonomy(
            p.raw_description,
            vendor=p.vendor.name if p.vendor else None,
            section_hint=section_hint,
            subset_canonical=subset,
        )
        proposal_rows.append({
            'p': p,
            'ili_count': n,
            'inferred': inferred,
        })

    if sort_by == 'frequency':
        proposal_rows.sort(key=lambda r: (-r['ili_count'], -r['p'].created_at.timestamp()))
    else:
        proposal_rows.sort(key=lambda r: -r['p'].created_at.timestamp())

    proposal_rows = proposal_rows[:100]

    status_counts = dict(
        ProductMappingProposal.objects.values('status').annotate(n=Count('id'))
        .values_list('status', 'n')
    )
    vendors = list(Vendor.objects.order_by('name').values_list('name', flat=True))
    all_canonicals = list(Product.objects.order_by('canonical_name')
                          .values_list('canonical_name', flat=True))
    # Distinct categories already in DB — for the inline-create form's category dropdown
    categories = sorted({c for c in Product.objects.values_list('category', flat=True).distinct() if c})

    return render(request, 'myapp/mapping_review.html', {
        'proposals': proposal_rows,
        'status_filter': status_filter,
        'vendor_filter': vendor_filter,
        'sort_by': sort_by,
        'status_counts': status_counts,
        'vendors': vendors,
        'all_canonicals': all_canonicals,
        'categories': categories,
    })


@require_POST
def mapping_review_approve(request, proposal_id: int):
    """POST endpoint: approve a proposal. Optional 'override_canonical'
    POST param to pick a different Product than the suggestion."""
    from .models import ProductMappingProposal
    proposal = get_object_or_404(ProductMappingProposal, id=proposal_id)
    if proposal.status != 'pending':
        messages.warning(request, f"Proposal #{proposal.id} already {proposal.status}.")
        return redirect('mapping_review')

    override_name = (request.POST.get('override_canonical') or '').strip()
    notes = (request.POST.get('notes') or '').strip()
    final_product = None
    if override_name:
        final_product = Product.objects.filter(canonical_name=override_name).first()
        if final_product is None:
            messages.error(request,
                f"Canonical {override_name!r} doesn't exist as a Product. "
                f"Create it first via /admin/myapp/product/add/ then re-approve.")
            return redirect('mapping_review')
    else:
        final_product = proposal.suggested_product

    if final_product is None:
        messages.error(request,
            f"Proposal #{proposal.id} has no suggested product and no override given.")
        return redirect('mapping_review')

    result = proposal.approve(
        product=final_product,
        reviewer=request.user if request.user.is_authenticated else None,
        notes=notes,
    )
    messages.success(request,
        f"Approved → {final_product.canonical_name!r} | "
        f"backfilled {result['ili_updated']} ILI rows | "
        f"ProductMapping {('updated' if result['product_mapping']._state.adding is False else 'created')}.")
    return redirect('mapping_review')


@require_POST
def mapping_review_reject(request, proposal_id: int):
    """POST endpoint: reject a proposal. Optional 'notes' POST param."""
    from .models import ProductMappingProposal
    proposal = get_object_or_404(ProductMappingProposal, id=proposal_id)
    if proposal.status != 'pending':
        messages.warning(request, f"Proposal #{proposal.id} already {proposal.status}.")
        return redirect('mapping_review')
    notes = (request.POST.get('notes') or '').strip()
    proposal.reject(
        reviewer=request.user if request.user.is_authenticated else None,
        notes=notes,
    )
    messages.info(request, f"Rejected proposal #{proposal.id}.")
    return redirect('mapping_review')


@require_POST
def mapping_review_create_and_approve(request, proposal_id: int):
    """POST endpoint: CREATE a new Product from inline form fields, then
    approve the proposal pointing at it. Lets reviewers add new canonicals
    without leaving the queue.

    Required POST params:
      canonical_name        — must be unique (rejected if exists)
      category              — Product.category
      primary_descriptor    — Product.primary_descriptor
      secondary_descriptor  — Product.secondary_descriptor (optional)
    """
    from .models import ProductMappingProposal
    proposal = get_object_or_404(ProductMappingProposal, id=proposal_id)
    if proposal.status != 'pending':
        messages.warning(request, f"Proposal #{proposal.id} already {proposal.status}.")
        return redirect('mapping_review')

    canonical = (request.POST.get('canonical_name') or '').strip()
    category = (request.POST.get('category') or '').strip()
    primary = (request.POST.get('primary_descriptor') or '').strip()
    secondary = (request.POST.get('secondary_descriptor') or '').strip()

    if not canonical:
        messages.error(request, 'canonical_name is required.')
        return redirect('mapping_review')

    # Check for collision — don't double-create
    existing = Product.objects.filter(canonical_name=canonical).first()
    if existing is not None:
        messages.warning(request,
            f"Canonical {canonical!r} already exists — using existing Product.")
        product = existing
    else:
        product = Product.objects.create(
            canonical_name=canonical,
            category=category,
            primary_descriptor=primary,
            secondary_descriptor=secondary,
        )
        messages.success(request,
            f"Created Product {canonical!r} ({category}/{primary}/{secondary}).")

    # Now approve the proposal pointing at the new (or existing) Product
    result = proposal.approve(
        product=product,
        reviewer=request.user if request.user.is_authenticated else None,
        notes=f'Created via inline form. Inferred fields: {category}/{primary}/{secondary}',
    )
    messages.success(request,
        f"Approved → {canonical!r} | backfilled {result['ili_updated']} ILI rows.")
    return redirect('mapping_review')


# ---- Mapper health dashboard (Step 2 of sheet→DB migration follow-up) ----

def mapping_health(request):
    """Live mapper-quality dashboard — answers 'is mapping working today?'
    in 30 seconds.

    Complements `pipeline_health` (broad cron + cache + recipe coverage)
    by focusing exclusively on the mapper FK chain. Surfaces the two
    silent-failure classes the mapper hardening was designed to catch:
    sheet/DB drift (`unmatched_drift` confidence tier) and fuzzy false
    positives (token-overlap zero rows in the mapped set).

    Cheap to render — pure DB aggregates + one filesystem stat. Safe to
    hit on every page load."""
    from collections import Counter
    from datetime import datetime as _datetime, timedelta
    from django.db.models import Count
    from pathlib import Path
    from django.conf import settings as _settings
    import os, re, time

    today = date.today()
    last_24h = _datetime.now() - timedelta(hours=24)
    last_7d = today - timedelta(days=7)

    # --- Coverage KPIs ---
    total_ili = InvoiceLineItem.objects.count()
    mapped = InvoiceLineItem.objects.filter(product__isnull=False).count()
    mapped_pct = round(mapped / total_ili * 100, 1) if total_ili else 0

    # Last-24h activity (uses imported_at — when the row landed in DB)
    last_24h_total = InvoiceLineItem.objects.filter(imported_at__gte=last_24h).count()
    last_24h_mapped = InvoiceLineItem.objects.filter(
        imported_at__gte=last_24h, product__isnull=False
    ).count()
    last_24h_pct = (round(last_24h_mapped / last_24h_total * 100, 1)
                    if last_24h_total else None)

    # --- Drift counter (Phase 0 signal) ---
    drift_count = InvoiceLineItem.objects.filter(
        match_confidence='unmatched_drift'
    ).count()

    # --- Recent unmapped queue (last 10 by imported_at) ---
    recent_unmapped = list(InvoiceLineItem.objects.filter(
        product__isnull=True,
        match_confidence__in=['unmatched', 'unmatched_drift', ''],
    ).select_related('vendor').order_by('-imported_at')[:10])

    # --- Recent drift queue ---
    recent_drift = list(InvoiceLineItem.objects.filter(
        match_confidence='unmatched_drift'
    ).select_related('vendor').order_by('-imported_at')[:10])

    # --- Live suspect-mappings count (zero token overlap among mapped) ---
    # Replicate audit_suspect_mappings logic: tokens from raw_description
    # that share at least one 3+letter content word with canonical_name.
    word_re = re.compile(r'[A-Za-z]{3,}')
    def _stem(s):
        out = []
        for t in word_re.findall(s or ''):
            low = t.lower()
            if len(low) >= 4 and low.endswith('s') and not low.endswith('ss'):
                low = low[:-1]
            out.append(low)
        return set(out)

    suspect_count = 0
    sample_suspects = []
    mapped_qs = (InvoiceLineItem.objects
                 .filter(product__isnull=False)
                 .select_related('product', 'vendor')
                 .only('id', 'raw_description', 'vendor__name',
                       'product__canonical_name', 'match_confidence'))
    for ili in mapped_qs.iterator():
        if not ili.raw_description or not ili.product:
            continue
        if not (_stem(ili.raw_description) & _stem(ili.product.canonical_name)):
            suspect_count += 1
            if len(sample_suspects) < 8:
                sample_suspects.append({
                    'id': ili.id,
                    'vendor': ili.vendor.name if ili.vendor else '',
                    'raw': (ili.raw_description or '')[:60],
                    'canonical': ili.product.canonical_name,
                    'confidence': ili.match_confidence,
                })

    suspect_pct = round(suspect_count / mapped * 100, 1) if mapped else 0

    # --- Match confidence breakdown (live) ---
    conf_rows_qs = (InvoiceLineItem.objects
                    .values('match_confidence')
                    .annotate(n=Count('id'))
                    .order_by('-n'))
    confidence_rows = []
    AUTO_COMMIT = {'code', 'vendor_exact', 'exact', 'non_product'}
    for r in conf_rows_qs:
        conf = r['match_confidence'] or '(blank)'
        confidence_rows.append({
            'label': conf,
            'n': r['n'],
            'pct': round(r['n'] / total_ili * 100, 1) if total_ili else 0,
            'is_auto_commit': conf in AUTO_COMMIT,
            'is_drift': conf == 'unmatched_drift',
            'is_unmatched': conf in ('unmatched', '', '(blank)'),
        })

    # --- Cache freshness ---
    cache_path = Path(_settings.BASE_DIR) / 'invoice_processor' / 'mappings' / 'item_mappings.json'
    cache_age_min = None
    cache_source = 'unknown'
    if cache_path.exists():
        cache_age_min = round((time.time() - cache_path.stat().st_mtime) / 60, 1)
        # Source: post-Step-2, refreshes from DB. Sheet fallback only when DB empty.
        from myapp.models import ProductMapping
        cache_source = 'DB' if ProductMapping.objects.exists() else 'sheet (fallback)'

    # --- Last batch run ---
    log_dir = Path(_settings.BASE_DIR) / 'logs'
    last_batch = None
    if log_dir.exists():
        batch_logs = sorted(log_dir.glob('invoice_batch_*.log'),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        if batch_logs:
            last_batch_mtime = batch_logs[0].stat().st_mtime
            last_batch = {
                'name': batch_logs[0].name,
                'minutes_ago': round((time.time() - last_batch_mtime) / 60),
            }

    # --- Health verdict (traffic light) ---
    if mapped_pct >= 90 and drift_count == 0 and suspect_pct < 5:
        verdict = 'green'
        verdict_text = 'Healthy'
    elif mapped_pct >= 75 and drift_count < 10 and suspect_pct < 15:
        verdict = 'amber'
        verdict_text = 'Watch'
    else:
        verdict = 'red'
        verdict_text = 'Attention needed'

    return render(request, 'myapp/mapping_health.html', {
        'today': today,
        'verdict': verdict,
        'verdict_text': verdict_text,
        'total_ili': total_ili,
        'mapped': mapped,
        'mapped_pct': mapped_pct,
        'last_24h_total': last_24h_total,
        'last_24h_mapped': last_24h_mapped,
        'last_24h_pct': last_24h_pct,
        'drift_count': drift_count,
        'suspect_count': suspect_count,
        'suspect_pct': suspect_pct,
        'sample_suspects': sample_suspects,
        'recent_unmapped': recent_unmapped,
        'recent_drift': recent_drift,
        'confidence_rows': confidence_rows,
        'cache_age_min': cache_age_min,
        'cache_source': cache_source,
        'last_batch': last_batch,
    })


# ---- Recipe cost-coverage dashboard (Phase 5 of cost-accuracy push) ----

def cost_coverage(request):
    """Per-RecipeIngredient classification of why each does/doesn't price.

    The 2026-04-22 cost-accuracy push lifted coverage from 19% → 57%; this
    view is the live audit — coverage moves up or down as new invoices
    arrive, parsers change, or recipe data is edited. Drift between visits
    flags either a regression (a fix broke something) or an improvement
    (a new invoice unlocked a previously-blocked product).

    Buckets: priced / no_invoice / unparseable_cs / incompat / no_density /
    other. Per-vendor table tracks where each vendor's coverage stands.
    Worst-covered recipes surface places to fix data first.

    Performance: ~398 RIs × 1 InvoiceLineItem lookup each = ~400 queries.
    Acceptable for an audit page; not in any hot path.
    """
    from .models import InvoiceLineItem, RecipeIngredient, Recipe
    from collections import Counter, defaultdict

    buckets = Counter()
    bucket_products: dict[str, Counter] = defaultdict(Counter)
    vendor_total: dict[str, int] = defaultdict(int)
    vendor_priced: dict[str, int] = defaultdict(int)
    recipe_total: dict[int, int] = defaultdict(int)
    recipe_priced: dict[int, int] = defaultdict(int)
    sample_failures: dict[str, list[dict]] = defaultdict(list)

    for ri in (RecipeIngredient.objects
               .filter(quantity__isnull=False, product__isnull=False)
               .select_related('product', 'recipe')):
        recipe_total[ri.recipe_id] += 1
        cost, note = ri.estimated_cost()
        # Find the latest invoice we used (for vendor attribution)
        li = (InvoiceLineItem.objects
              .filter(product=ri.product, unit_price__gt=0)
              .select_related('vendor').order_by('-invoice_date').first())
        v = (li.vendor.name if li and li.vendor else '—')
        vendor_total[v] += 1

        if cost is not None:
            buckets['priced'] += 1
            vendor_priced[v] += 1
            recipe_priced[ri.recipe_id] += 1
            continue

        if 'no invoice' in note:
            bucket = 'no_invoice'
        elif 'unparseable case_size' in note:
            bucket = 'unparseable_cs'
        elif 'no density' in note:
            bucket = 'no_density'
        elif 'incompatible' in note or 'conversion failed' in note:
            bucket = 'incompat'
        else:
            bucket = 'other'

        buckets[bucket] += 1
        bucket_products[bucket][ri.product.canonical_name] += 1
        if len(sample_failures[bucket]) < 8:
            sample_failures[bucket].append({
                'recipe': ri.recipe.name,
                'product': ri.product.canonical_name,
                'qty': ri.quantity,
                'unit': ri.unit,
                'case_size': li.case_size if li else '',
                'note': note,
            })

    total = sum(buckets.values()) or 1
    bucket_rows = [
        {'name': b, 'count': n, 'pct': round(100 * n / total, 1)}
        for b, n in buckets.most_common()
    ]
    vendor_rows = [
        {'name': v, 'total': vt, 'priced': vendor_priced[v],
         'pct': round(100 * vendor_priced[v] / vt, 1) if vt else 0}
        for v, vt in sorted(vendor_total.items(), key=lambda x: -x[1])
    ]

    # Worst-covered recipes (with at least 3 ingredients)
    worst = []
    recipes_by_id = {r.id: r for r in Recipe.objects.filter(
        id__in=[rid for rid in recipe_total if recipe_total[rid] >= 3])}
    for rid, t in recipe_total.items():
        if t < 3 or rid not in recipes_by_id:
            continue
        p = recipe_priced.get(rid, 0)
        worst.append({
            'recipe': recipes_by_id[rid],
            'priced': p,
            'total': t,
            'pct': round(100 * p / t, 1),
        })
    worst.sort(key=lambda r: (r['pct'], -r['total']))

    # Top blocking products per bucket (for "what to fix next")
    blocking = {
        b: [{'product': p, 'count': n} for p, n in c.most_common(8)]
        for b, c in bucket_products.items()
    }

    return render(request, 'myapp/cost_coverage.html', {
        'today': date.today(),
        'total_eligible': total,
        'priced': buckets.get('priced', 0),
        'priced_pct': round(100 * buckets.get('priced', 0) / total, 1),
        'bucket_rows': bucket_rows,
        'vendor_rows': vendor_rows,
        'worst_recipes': worst[:15],
        'blocking': blocking,
        'sample_failures': dict(sample_failures),
    })


# ---- Category spend/activity dashboard ----

def category_spend(request):
    """Line-item activity + estimated spend by Product.category, with a 4-month
    trend. Companion to /cogs/ — that view shows authoritative invoice totals;
    this view shows category-level distribution.

    Estimated spend is Σ(unit_price) per category — NOT authoritative dollars
    (case-count per line isn't stored). Directional only; trends and ratios
    between categories are reliable."""
    from django.db.models import Count, Sum
    from django.db.models.functions import TruncMonth
    from calendar import monthrange
    from collections import defaultdict

    today = date.today()
    year, month = today.year, today.month

    # --- Current month breakdown ---
    mo_start = date(year, month, 1)
    mo_end = date(year, month, monthrange(year, month)[1])

    current = list(InvoiceLineItem.objects
                   .filter(product__isnull=False,
                           invoice_date__gte=mo_start, invoice_date__lte=mo_end)
                   .values('product__category')
                   .annotate(
                       line_count=Count('id'),
                       est_spend=Sum('unit_price'),
                       unique_products=Count('product', distinct=True),
                   )
                   .order_by('-line_count'))

    # Normalize missing-category
    for row in current:
        row['category'] = row['product__category'] or '(uncategorized)'
        row['est_spend'] = row['est_spend'] or Decimal('0')

    total_lines = sum(r['line_count'] for r in current) or 1
    # Start accumulator at Decimal('0') so empty-DB path doesn't degrade
    # to int(0), which breaks the .quantize() call downstream.
    total_est_spend = sum((r['est_spend'] for r in current), Decimal('0'))

    for row in current:
        row['pct_of_lines'] = (Decimal(row['line_count']) / Decimal(total_lines) * 100).quantize(Decimal('0.1'))
        row['pct_of_est_spend'] = ((row['est_spend'] / total_est_spend * 100).quantize(Decimal('0.1'))
                                   if total_est_spend else Decimal('0'))

    # --- Top products per category (current month) ---
    top_products: dict = {}
    for row in current:
        cat = row['product__category']
        if not cat:
            continue
        tops = (InvoiceLineItem.objects
                .filter(product__category=cat,
                        invoice_date__gte=mo_start, invoice_date__lte=mo_end)
                .values('product__canonical_name')
                .annotate(n=Count('id'), spend=Sum('unit_price'))
                .order_by('-n')[:3])
        top_products[cat] = [
            {'name': t['product__canonical_name'],
             'n': t['n'],
             'spend': (t['spend'] or Decimal('0')).quantize(Decimal('0.01'))}
            for t in tops
        ]

    # --- 4-month trend: lines per category over last 4 months ---
    trend_months = []
    for delta in range(3, -1, -1):
        y, m = year, month - delta
        while m <= 0:
            y -= 1
            m += 12
        start = date(y, m, 1)
        end = date(y, m, monthrange(y, m)[1])
        trend_months.append({'year': y, 'month': m, 'label': start.strftime('%b %Y'),
                             'start': start, 'end': end})

    # For trend: pick top 6 categories from current month (or overall recent period)
    recent_cats = [r['product__category'] for r in current[:6] if r['product__category']]
    # If current month has few rows, fall back to all-time top
    if len(recent_cats) < 4:
        fallback = (InvoiceLineItem.objects
                    .filter(product__isnull=False, invoice_date__gte=trend_months[0]['start'])
                    .values('product__category')
                    .annotate(n=Count('id'))
                    .order_by('-n')[:6])
        recent_cats = [r['product__category'] for r in fallback if r['product__category']]

    trend_rows = []
    max_val = 0
    for cat in recent_cats:
        cells = []
        for tm in trend_months:
            n = (InvoiceLineItem.objects
                 .filter(product__category=cat,
                         invoice_date__gte=tm['start'], invoice_date__lte=tm['end'])
                 .count())
            cells.append({'label': tm['label'], 'n': n})
            max_val = max(max_val, n)
        trend_rows.append({'category': cat, 'cells': cells})

    return render(request, 'myapp/category_spend.html', {
        'current_label': today.strftime('%B %Y'),
        'current': current,
        'top_products': top_products,
        'trend_months': trend_months,
        'trend_rows': trend_rows,
        'trend_max': max_val or 1,
        'total_lines': total_lines,
        'total_est_spend': total_est_spend.quantize(Decimal('0.01')),
    })


# ---- Dish suggestions ----

MEAL_FOLDER_HEURISTIC = ('Proteins/', 'Composed Meals/', 'Breakfast/', 'Side Dishes/', 'Events/')


def _candidate_recipes_for_slot(slot: str):
    """Candidate pool: recipes that are 'meal-level' by either having been linked
    to a Menu row in history, or living under a meal-appropriate folder.
    Sub-recipes (Prep Components, Baking components) are excluded."""
    linked_ids = set(Menu.objects.filter(recipe__isnull=False)
                                 .values_list('recipe_id', flat=True))
    through_m2m = set(Menu.objects.values_list('additional_recipes__id', flat=True)
                                  .exclude(additional_recipes__isnull=True))
    linked_ids |= through_m2m

    import re as _re
    folder_q = models.Q()
    for folder in MEAL_FOLDER_HEURISTIC:
        folder_q |= models.Q(source_doc__icontains=folder)
    pool = (Recipe.objects.filter(models.Q(id__in=linked_ids) | folder_q)
                          .distinct()
                          .prefetch_related('ingredients'))
    # Slot-specific filter: breakfasts favor Breakfast/ folder.
    if slot in ('cold_breakfast', 'hot_breakfast'):
        pool = pool.filter(models.Q(id__in=linked_ids) |
                           models.Q(source_doc__icontains='Breakfast/'))
    # Respect the explicit valid_slots tag when set (permissive when empty).
    # Python-side filter — SQLite JSONField doesn't support __contains on arrays.
    if slot:
        pool = [r for r in pool if not r.valid_slots or slot in r.valid_slots]
    return pool


def _score_candidate(recipe: Recipe, target_date: date, slot: str,
                     neighbor_proteins: dict, recent_dates: dict) -> tuple[int, list[str]]:
    """Return (score, [reasons_for_display]).
    neighbor_proteins: {(date, slot): protein_str} for nearby menu slots
    recent_dates: {recipe_id: most_recent_menu_date}  (None if never served)"""
    score = 0
    reasons = []

    # --- Recency
    last = recent_dates.get(recipe.id)
    if last is None:
        score += 4
        reasons.append('never served')
    else:
        days_since = (target_date - last).days
        if days_since >= 30:
            score += 4
            reasons.append(f'not in {days_since}d')
        elif days_since >= 14:
            score += 3
            reasons.append(f'not in {days_since}d')
        elif days_since >= 7:
            score += 1
            reasons.append(f'last {days_since}d ago')
        else:
            score -= 3
            reasons.append(f'served {days_since}d ago')

    # --- Protein diversity (vs yesterday's dinner, today's lunch, today's other slots)
    my_p = recipe.protein or ''
    if my_p:
        # Yesterday's dinner
        yest_p = neighbor_proteins.get((target_date - timedelta(days=1), 'dinner'), '')
        if yest_p and yest_p == my_p:
            score -= 4
            reasons.append(f'same protein as yesterday dinner')
        # Today's other slots
        for other_slot in ('cold_breakfast', 'hot_breakfast', 'lunch', 'dinner'):
            if other_slot == slot:
                continue
            other_p = neighbor_proteins.get((target_date, other_slot), '')
            if other_p and other_p == my_p:
                score -= 2
                reasons.append(f'same protein as today {other_slot.replace("_", " ")}')
                break

    # --- Fat/health alternation
    my_fh = recipe.fat_health
    if my_fh:
        # Check the previous day's dinner's fat/health
        for other_slot in ('lunch', 'dinner'):
            if other_slot == slot:
                continue
            menu_other = Menu.objects.filter(date=target_date, meal_slot=other_slot,
                                             recipe__isnull=False).select_related('recipe').first()
            if menu_other and menu_other.recipe.fat_health == my_fh:
                score -= 1
                reasons.append(f'same F/H as today {other_slot}')
                break

    # --- Popularity: prefer learned rate when we have enough samples
    if recipe.learned_consumption_rate is not None and recipe.learned_sample_count >= 3:
        rate = float(recipe.learned_consumption_rate)
        if rate >= 0.92:
            score += 4
            reasons.append(f'learned 👍 {rate:.0%} over {recipe.learned_sample_count}x')
        elif rate >= 0.80:
            score += 2
            reasons.append(f'learned OK {rate:.0%} over {recipe.learned_sample_count}x')
        elif rate >= 0.65:
            score -= 1
            reasons.append(f'learned meh {rate:.0%} over {recipe.learned_sample_count}x')
        else:
            score -= 3
            reasons.append(f'learned 👎 {rate:.0%} over {recipe.learned_sample_count}x')
    else:
        # Fall back to manual Menu-Guide popularity tag
        if recipe.popularity == 'high':
            score += 3
            reasons.append('popular 👍')
        elif recipe.popularity == 'medium':
            score += 1
        elif recipe.popularity == 'low':
            score -= 2
            reasons.append('low popularity')

    return score, reasons


def menu_suggestions(request):
    """List dish suggestions ranked for a given (date, slot).
    Query params: ?date=YYYY-MM-DD&slot=lunch  (defaults: today, dinner)."""
    try:
        target = (date.fromisoformat(request.GET['date'])
                  if request.GET.get('date') else date.today())
    except ValueError:
        target = date.today()
    slot = request.GET.get('slot') or 'dinner'
    if slot not in dict(Menu.MEAL_SLOTS):
        slot = 'dinner'

    # Neighbor proteins: 3 days around target
    neighbor_menus = (Menu.objects
                      .filter(date__gte=target - timedelta(days=3),
                              date__lte=target + timedelta(days=1))
                      .select_related('recipe')
                      .prefetch_related('additional_recipes'))
    neighbor_proteins: dict = {}
    for m in neighbor_menus:
        if m.recipe and m.recipe.protein:
            neighbor_proteins[(m.date, m.meal_slot)] = m.recipe.protein
        else:
            for r in m.additional_recipes.all():
                if r.protein:
                    neighbor_proteins[(m.date, m.meal_slot)] = r.protein
                    break

    # Recent dates per recipe_id
    recent_dates: dict = {}
    for m in Menu.objects.filter(date__lte=target,
                                 date__gte=target - timedelta(days=90)):
        ids = []
        if m.recipe_id:
            ids.append(m.recipe_id)
        ids.extend(m.additional_recipes.values_list('id', flat=True))
        for rid in ids:
            prev = recent_dates.get(rid)
            if prev is None or m.date > prev:
                recent_dates[rid] = m.date

    candidates = list(_candidate_recipes_for_slot(slot))
    scored = []
    for r in candidates:
        score, reasons = _score_candidate(r, target, slot, neighbor_proteins, recent_dates)
        scored.append({
            'recipe': r,
            'score': score,
            'reasons': reasons,
            'last_served': recent_dates.get(r.id),
            'has_conflicts': bool(r.conflicts),
        })
    scored.sort(key=lambda s: (-s['score'], s['recipe'].name.lower()))

    return render(request, 'myapp/suggestions.html', {
        'target_date': target,
        'slot': slot,
        'slot_label': dict(Menu.MEAL_SLOTS).get(slot, slot.title()),
        'suggestions': scored[:20],
        'total_candidates': len(candidates),
    })


# ---- COGs / Budget Dashboard ----

BUDGET_PER_RESIDENT_PER_MONTH = Decimal('346.67')
INVOICE_TOTALS_DIR = 'invoice_totals'  # at project root, under ".invoice_totals/"


def _load_invoice_totals_for_month(year: int, month: int) -> list[dict]:
    """Read .invoice_totals/YYYY-MM.json; return [] if file absent."""
    import json
    from django.conf import settings as _settings
    base = _settings.BASE_DIR / '.invoice_totals'
    path = base / f'{year:04d}-{month:02d}.json'
    if not path.exists():
        return []
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


# Historical monthly totals known from the budget CSV / reconciliation.
# Sourced from Men's Wentworth Food Budget 2026(Mar).csv + memory's
# reconciliation figures. Used until full monthly caches exist.
HISTORICAL_ACTUAL_SPEND = {
    (2026, 1): Decimal('13431.00'),   # from project_budget_sheet.md
    (2026, 2): Decimal('8191.00'),    # from project_budget_sheet.md
    (2026, 3): Decimal('12870.38'),   # from budget CSV
}
HISTORICAL_CENSUS = {
    (2026, 1): Decimal('26'),
    (2026, 2): Decimal('23'),
    (2026, 3): Decimal('30.38'),
}


def _month_spend(year: int, month: int) -> tuple[Decimal, list[dict]]:
    """Return (total_dollars, invoice_list) for a given month.
    Prefers .invoice_totals/ cache; falls back to HISTORICAL_ACTUAL_SPEND if cache is empty."""
    entries = _load_invoice_totals_for_month(year, month)
    if entries:
        total = sum(Decimal(str(e.get('total', 0))) for e in entries)
        return total.quantize(Decimal('0.01')), entries
    historical = HISTORICAL_ACTUAL_SPEND.get((year, month))
    if historical is not None:
        return historical, []
    return Decimal('0.00'), []


def _default_census_for(year: int, month: int) -> Decimal:
    """Average headcount for the month. Uses Census table if populated,
    else HISTORICAL_CENSUS, else most-recent Census row."""
    from calendar import monthrange
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])
    rows = Census.objects.filter(date__gte=start, date__lte=end)
    if rows.exists():
        vals = [r.headcount for r in rows]
        return Decimal(sum(vals)) / Decimal(len(vals))
    historical = HISTORICAL_CENSUS.get((year, month))
    if historical is not None:
        return historical
    latest = Census.objects.order_by('-date').first()
    return Decimal(latest.headcount) if latest else Decimal('30')


def cogs_dashboard(request):
    """Food-spend vs budget dashboard. Current-month status + 4-month trend.
    Query params ?year=YYYY&month=MM let you view a past month; defaults to today.
    """
    today = date.today()
    try:
        year = int(request.GET.get('year', today.year))
        month = int(request.GET.get('month', today.month))
    except (TypeError, ValueError):
        year, month = today.year, today.month
    if not (1 <= month <= 12):
        year, month = today.year, today.month
    is_current = (year == today.year and month == today.month)
    is_future = (year, month) > (today.year, today.month)
    if is_future:
        year, month = today.year, today.month
        is_current = True

    # --- Selected month ---
    cache_spend, current_invoices = _month_spend(year, month)
    current_census = _default_census_for(year, month)
    current_budget = (current_census * BUDGET_PER_RESIDENT_PER_MONTH).quantize(Decimal('0.01'))

    # When a reconciled budget-sheet total exists and the pipeline cache is
    # materially short of it (common for months where we have no Sysco OCR),
    # use the reconciled figure as the headline spend. The vendor breakdown
    # + invoice list below still come from what the pipeline captured.
    reconciled_total = HISTORICAL_ACTUAL_SPEND.get((year, month))
    partial_cache = (
        reconciled_total is not None
        and current_invoices
        and cache_spend < reconciled_total * Decimal('0.80')
    )
    if partial_cache:
        current_spend = reconciled_total
    else:
        current_spend = cache_spend
    current_delta = current_budget - current_spend  # positive = under budget

    # Pace / days-elapsed only meaningful for the current month; past months
    # use the full month length so $/resident/day is a whole-month average.
    from calendar import monthrange
    days_in_month = monthrange(year, month)[1]
    if is_current:
        days_elapsed = today.day
    else:
        days_elapsed = days_in_month
    elapsed_pct = Decimal(days_elapsed) / Decimal(days_in_month)
    budget_pace = (current_budget * elapsed_pct).quantize(Decimal('0.01'))
    pace_delta = budget_pace - current_spend  # positive = spending slower than linear pace

    # Per-resident-per-day metrics
    target_per_res_per_day = BUDGET_PER_RESIDENT_PER_MONTH / Decimal('30')
    actual_per_res_per_day = (
        (current_spend / current_census / Decimal(days_elapsed)).quantize(Decimal('0.01'))
        if current_census and days_elapsed else Decimal('0.00')
    )

    # Vendor breakdown for current month
    from collections import Counter
    vendor_totals = Counter()
    for inv in current_invoices:
        vendor_totals[inv.get('vendor', 'Unknown')] += Decimal(str(inv.get('total', 0)))
    vendor_rows = [
        {'vendor': v, 'total': t.quantize(Decimal('0.01')),
         'pct': (t / current_spend * 100).quantize(Decimal('0.1')) if current_spend else Decimal('0')}
        for v, t in sorted(vendor_totals.items(), key=lambda x: -x[1])
    ]

    # --- Trend: last 4 months (selected month + 3 back) ---
    trend_rows = []
    for delta in range(3, -1, -1):
        y, m = year, month - delta
        while m <= 0:
            y -= 1
            m += 12
        spend, _ = _month_spend(y, m)
        census = _default_census_for(y, m)
        budget = (census * BUDGET_PER_RESIDENT_PER_MONTH).quantize(Decimal('0.01'))
        trend_rows.append({
            'year': y, 'month': m,
            'label': date(y, m, 1).strftime('%b %Y'),
            'spend': spend,
            'budget': budget,
            'census': census.quantize(Decimal('0.01')),
            'delta': (budget - spend).quantize(Decimal('0.01')),
            'is_selected': (y == year and m == month),
            'is_today_month': (y == today.year and m == today.month),
            'spend_pct_of_budget': (spend / budget * 100).quantize(Decimal('0.1')) if budget else Decimal('0'),
        })

    # Prev / next month for nav links
    prev_y, prev_m = (year, month - 1) if month > 1 else (year - 1, 12)
    next_y, next_m = (year, month + 1) if month < 12 else (year + 1, 1)
    # Don't offer "next" past the current month
    has_next = (next_y, next_m) <= (today.year, today.month)

    # Max spend in trend for bar scaling
    max_trend = max([r['spend'] for r in trend_rows] + [r['budget'] for r in trend_rows],
                    default=Decimal('1'))

    # Recent invoices (current month, descending by date)
    recent_invoices = sorted(
        current_invoices,
        key=lambda x: x.get('date', ''),
        reverse=True,
    )[:15]

    return render(request, 'myapp/cogs.html', {
        'today': today,
        'current_label': date(year, month, 1).strftime('%B %Y'),
        'is_current_month': is_current,
        'year': year,
        'month': month,
        'prev_year': prev_y,
        'prev_month': prev_m,
        'next_year': next_y,
        'next_month': next_m,
        'has_next': has_next,
        'current_spend': current_spend,
        'current_budget': current_budget,
        'current_delta': current_delta,
        'current_census': current_census.quantize(Decimal('0.01')),
        'days_elapsed': days_elapsed,
        'days_in_month': days_in_month,
        'elapsed_pct': (elapsed_pct * 100).quantize(Decimal('0.1')),
        'budget_pace': budget_pace,
        'pace_delta': pace_delta,
        'target_per_res_per_day': target_per_res_per_day.quantize(Decimal('0.01')),
        'actual_per_res_per_day': actual_per_res_per_day,
        'vendor_rows': vendor_rows,
        'trend_rows': trend_rows,
        'max_trend': max_trend,
        'recent_invoices': recent_invoices,
        'budget_per_resident_per_month': BUDGET_PER_RESIDENT_PER_MONTH,
        'reconciled_total': reconciled_total,
        'partial_cache': partial_cache,
        'cache_spend': cache_spend,
    })
