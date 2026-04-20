"""
Book of Yields (8e) PDF parsers.

Each section has its own table layout; per-section parsers handle the quirks.
Dispatch: PARSER_FOR_SECTION[section_key](page, book_page_num) -> list[ParsedRow]
"""
from .base import ParsedRow, page_words, group_lines
from .standard import parse_standard_page
from .seafood import parse_seafood_page
from .meats import parse_meats_page
from .poultry import parse_poultry_page


PARSER_FOR_SECTION = {
    # 3-col simple (Item | Fillet Yield % | Edible Oz/AP Pound)
    'seafood':       parse_seafood_page,
    # 7-col complex (NAMP | AP Wt lbs | Trim Loss | Primary-Use Yield | Yield % | Usable Oz/AP | Trim Misc)
    'meats':         parse_meats_page,
    # Hierarchical with parent rows (Chicken/Turkey/Duck as parents; Whole/Breast/etc as children)
    'poultry':       parse_poultry_page,
    # Standard 7-col (vegetables, fruit, canned, grains, etc.)
    'herbs_spices':  parse_standard_page,
    'fresh_herbs':   parse_standard_page,
    'vegetables':    parse_standard_page,
    'fruit':         parse_standard_page,
    'canned':        parse_standard_page,
    'dry_legumes':   parse_standard_page,
    'grains':        parse_standard_page,
    'pasta':         parse_standard_page,
    'nuts_seeds':    parse_standard_page,
    'flour':         parse_standard_page,
    'sweeteners':    parse_standard_page,
    'baking':        parse_standard_page,
    'fats_oils':     parse_standard_page,
    'condiments':    parse_standard_page,
    'liquids':       parse_standard_page,
    'dairy':         parse_standard_page,
    'beverages':     parse_standard_page,
}


__all__ = ['ParsedRow', 'PARSER_FOR_SECTION']
