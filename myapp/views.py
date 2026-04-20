from datetime import date, timedelta
from decimal import Decimal

from django.contrib import messages
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
    Census, IngredientSkipNote, Menu, MenuFreetextComponent, PrepTask, Product,
    Recipe, RecipeIngredient, YieldReference, PROTEIN_CHOICES,
    CONFLICT_LABELS, CONFLICT_ICONS,
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
    menu = get_object_or_404(Menu, pk=menu_id)
    return render(request, 'myapp/menu_detail.html', {'menu': menu})


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
        'all_recipes':   Recipe.objects.order_by('name'),
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
        'all_recipes':   Recipe.objects.order_by('name'),
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
    """Return (vendor, unit_price, case_size) from the most-recent InvoiceLineItem."""
    from myapp.models import InvoiceLineItem
    latest = (InvoiceLineItem.objects
              .filter(product=product)
              .order_by('-invoice_date')
              .select_related('vendor')
              .first())
    if not latest:
        return None, None, None
    return latest.vendor, latest.unit_price, latest.case_size


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

    # Bucket products by vendor using latest invoice
    from collections import defaultdict
    by_vendor: dict[str, list[dict]] = defaultdict(list)
    for pid, data in agg_by_product.items():
        vendor, unit_price, case_size = _latest_invoice_info(data['product'])
        vendor_name = vendor.name if vendor else '— unknown / no invoice history —'
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
    })


def kitchen_display(request):
    """Read-only, big-text kitchen display — designed for a wall-mounted tablet.
    Optional ?as_of=YYYY-MM-DD to preview any date (demo-friendly)."""
    as_of_str = request.GET.get('as_of')
    try:
        as_of = date.fromisoformat(as_of_str) if as_of_str else date.today()
    except ValueError:
        as_of = date.today()

    # Today's meals (if any)
    today_menus = list(Menu.objects
                       .filter(date=as_of)
                       .prefetch_related('additional_recipes', 'freetext_components')
                       .order_by('meal_slot'))
    # Build a slot-keyed map for guaranteed ordering
    slot_order = ['cold_breakfast', 'hot_breakfast', 'lunch', 'dinner']
    slot_labels = dict(Menu.MEAL_SLOTS)
    today_by_slot = {m.meal_slot: m for m in today_menus}
    today_rows = [
        {'slot': s, 'label': slot_labels.get(s, s.title()), 'menu': today_by_slot.get(s)}
        for s in slot_order
    ]

    # Next 6 days' highlights for the "coming up" strip
    upcoming_menus = (Menu.objects
                      .filter(date__gt=as_of, date__lte=as_of + timedelta(days=6))
                      .prefetch_related('additional_recipes')
                      .order_by('date', 'meal_slot'))
    # Group upcoming by date
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

    return render(request, 'myapp/display.html', {
        'as_of':         as_of,
        'is_today':      as_of == date.today(),
        'today_rows':    today_rows,
        'upcoming_days': upcoming_days,
        'census':        census,
        'has_any_today': bool(today_menus),
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
    return render(request, 'myapp/recipe_form.html', {
        'recipe': recipe, 'form': form, 'formset': formset,
    })


def _lineage_recipes(recipe: Recipe) -> list[Recipe]:
    """Return all recipes in the same version lineage (same trunk), including the
    given recipe. Walks parent chain to root, then BFS-collects all descendants."""
    # Find root
    root = recipe
    seen = {root.pk}
    while root.parent_recipe_id and root.parent_recipe_id not in seen:
        root = root.parent_recipe
        seen.add(root.pk)
    # BFS from root
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
    q = (request.GET.get('q') or '').strip()
    section = (request.GET.get('section') or '').strip()
    refs = YieldReference.objects.all().order_by('section', 'ingredient', 'prep_state')
    if q:
        refs = refs.filter(models.Q(ingredient__icontains=q) | models.Q(prep_state__icontains=q))
    if section:
        refs = refs.filter(section=section)

    total_count = YieldReference.objects.count()
    by_section = (
        YieldReference.objects.values('section').order_by('section')
        .annotate(n=models.Count('id'))
    )
    return render(request, 'myapp/yield_list.html', {
        'refs': refs[:500],        # cap for pagination sanity
        'q': q,
        'selected_section': section,
        'total_count': total_count,
        'by_section': list(by_section),
        'section_choices': YieldReference.SECTION_CHOICES,
        'shown_count': min(refs.count(), 500),
    })


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

    rows = []
    for row in unlinked:
        name_lc = row['name_lc']
        if _is_pantry_skip(name_lc):
            continue
        candidates = list(_candidate_yield_refs(name_lc))
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

    # --- Popularity
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
    """Food-spend vs budget dashboard. Current-month status + 4-month trend."""
    today = date.today()
    year, month = today.year, today.month

    # --- Current month ---
    current_spend, current_invoices = _month_spend(year, month)
    current_census = _default_census_for(year, month)
    current_budget = (current_census * BUDGET_PER_RESIDENT_PER_MONTH).quantize(Decimal('0.01'))
    current_delta = current_budget - current_spend  # positive = under budget

    # Days elapsed vs month length
    from calendar import monthrange
    days_in_month = monthrange(year, month)[1]
    days_elapsed = today.day
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

    # --- Trend: last 4 months (current + 3 back) ---
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
            'is_current': (y == year and m == month),
            'spend_pct_of_budget': (spend / budget * 100).quantize(Decimal('0.1')) if budget else Decimal('0'),
        })

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
        'current_label': today.strftime('%B %Y'),
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
    })
