"""Template context processors — globals available in every template."""
from myapp.models import Product

# Mirrors views._NUTRI_NONFOOD: categories excluded from the nutrition queue.
_NUTRI_NONFOOD = {"Smallwares", "Chemicals", "Pseudo"}


def nutrition_review_badge(request):
    """Count of food products still awaiting a nutrition match — drives the
    badge on the nav's Nutrition Review link. Cheap COUNT; fails soft to 0,
    and skips the query entirely for anonymous requests (the nav only renders
    for authenticated users)."""
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    try:
        n = (Product.objects
             .exclude(category__in=_NUTRI_NONFOOD)
             .filter(nutrition_confidence="")
             .count())
    except Exception:
        n = 0
    return {"nutrition_review_count": n}
