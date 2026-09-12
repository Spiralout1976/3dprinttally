"""Shared, finite numeric validation; free-text fields remain free text."""
import math
import re


class InputError(ValueError):
    pass


def number(value, label, minimum=0, maximum=1e9, integer=False, optional=True):
    if value is None or str(value).strip() == "":
        if optional:
            return None
        raise InputError(f"{label}: enter a number.")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise InputError(f"{label}: enter a valid number.") from None
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise InputError(f"{label}: enter a finite number between {minimum:g} and {maximum:g}.")
    if integer and result != int(result):
        raise InputError(f"{label}: enter a whole number.")
    return result


FIELDS = {name: (0, 1e9, False) for name in (
    'qty_on_plate filament_used_g filament_cost active_labor_min actual_selling_price '
    'assembly_labor_min qty_per_product nozzle_size layer_height labor_rate electricity_rate '
    'printer_watts machine_wear_rate sales_tax_rate default_active_labor_min design_rate '
    'min_job_charge selling_price_per_item other_job_cost design_minutes minutes '
    'default_spool_g reorder_threshold_g open_spools open_price_per_spool spools '
    'grams_per_spool price_per_spool waste_grams personal_grams actual_grams cost '
    'qty').split()}
FIELDS.update({
    'order_quantity': (1, 1000000, True), 'start_position': (1, 30, True),
    'copies[]': (1, 3000, True), 'qty[]': (1, 1000000, True),
    'default_wholesale_margin': (0, .99, False), 'default_retail_margin': (0, .99, False),
    'scrap_rate': (0, 50, False), 'sales_tax_rate': (0, 100, False),
    'x_offset_mm': (-100, 100, False), 'y_offset_mm': (-100, 100, False),
    'grams': (-1e9, 1e9, False),
})
for field in ('qty_on_plate','qty_per_product','default_spool_g','spools','grams_per_spool','minutes','qty'):
    FIELDS[field] = (.000001, 1e9, False)


def validate_form(form):
    from costing import parse_print_time
    for key, values in form.lists():
        bounds = FIELDS.get(key)
        if re.fullmatch(r'cp_(plate|qty_per|grams|cost|labor)_\d+', key):
            positive = key.startswith(('cp_plate_', 'cp_qty_per_'))
            bounds = (.000001 if positive else 0, 1e9, False)
        if re.fullmatch(r'(cp_)?filg_[a-zA-Z0-9]+_\d+', key):
            bounds = (0, 1e9, False)
        for value in values:
            if bounds:
                number(value, key.replace('_', ' '), *bounds)
            if key == 'print_time_text' or re.fullmatch(r'cp_time_\d+', key):
                if value.strip():
                    duration = parse_print_time(value)
                    if duration is None or not math.isfinite(duration) or not 0 < duration <= 525600:
                        raise InputError(f'{key}: enter a positive print time, e.g. 1h 50m.')
