import math
from config import DEFAULT_ACTIVE_LABOR_MIN
def trunc(value, decimals):
    # Matches Excel's TRUNC() — truncates toward zero, never rounds.
    factor = 10 ** decimals
    return math.trunc(value * factor) / factor


def parse_print_time(text):
    # Parses "1h 50m", "2h", "45m" etc. into total minutes. Returns None if unparseable.
    if not text:
        return None
    t = text.strip().lower()
    try:
        if "h" in t:
            h_part, rest = t.split("h", 1)
            hours = float(h_part.strip())
            minutes = 0.0
            rest = rest.replace("m", "").strip()
            if rest:
                minutes = float(rest)
            return hours * 60 + minutes
        if "m" in t:
            return float(t.replace("m", "").strip())
        return float(t)
    except ValueError:
        return None


def calculate_pricing(qty_on_plate, filament_used_g, filament_cost, print_time_text,
                       active_labor_min, actual_selling_price, settings):
    """Catalog-page costing. Truncates (does not round) every intermediate value.

    This preserves the spreadsheet lineage the tool replaced, where each cell
    truncated to three decimals before feeding the next. The difference from
    calculate_quick() below is pennies, but it is deliberate: catalog prices and
    quoted job prices came from two different sheets and were never identical, so
    both behaviors are kept rather than silently unified.

    Returns a dict of computed values, or a dict with 'error' set if required
    inputs are missing or invalid."""
    labor_rate = float(settings.get("labor_rate", 25))
    electricity_rate = float(settings.get("electricity_rate", 0.15))
    printer_watts = float(settings.get("printer_watts", 120))
    machine_wear_rate = float(settings.get("machine_wear_rate", 0.1))
    default_wholesale_margin = float(settings.get("default_wholesale_margin", 0.6))
    default_retail_margin = float(settings.get("default_retail_margin", 0.7))
    if not qty_on_plate or qty_on_plate <= 0:
        return {"error": "Qty on Plate must be greater than 0."}
    print_minutes = parse_print_time(print_time_text)
    if print_minutes is None or not math.isfinite(print_minutes) or print_minutes <= 0:
        return {"error": 'Print Time must look like "1h 50m", "2h", or "45m".'}
    filament_cost = filament_cost or 0.0
    if active_labor_min in (None, ""):
        labor_min = DEFAULT_ACTIVE_LABOR_MIN
    else:
        labor_min = float(active_labor_min)
    electricity_batch = trunc((print_minutes / 60) * (printer_watts / 1000) * electricity_rate, 3)
    machine_wear_batch = trunc((print_minutes / 60) * machine_wear_rate, 3)
    labor_batch = trunc((labor_min / 60) * labor_rate, 3)
    total_batch_cost = trunc(filament_cost + electricity_batch + machine_wear_batch + labor_batch, 3)
    true_cost = trunc(total_batch_cost / qty_on_plate, 3)
    result = {
        "print_minutes": print_minutes,
        "electricity_batch": electricity_batch,
        "machine_wear_batch": machine_wear_batch,
        "labor_batch": labor_batch,
        "total_batch_cost": total_batch_cost,
        "true_cost_per_item": true_cost,
        "suggested_wholesale": trunc(true_cost / (1 - default_wholesale_margin), 2),
        "margin_40": trunc(true_cost / (1 - 0.40), 2),
        "margin_50": trunc(true_cost / (1 - 0.50), 2),
        "margin_60": trunc(true_cost / (1 - 0.60), 2),
        "margin_70": trunc(true_cost / (1 - 0.70), 2),
        "margin_80": trunc(true_cost / (1 - 0.80), 2),
        "suggested_retail": trunc(true_cost / (1 - default_retail_margin), 2),
        "actual_profit": None,
        "actual_margin": None,
    }
    if actual_selling_price:
        asp = float(actual_selling_price)
        result["actual_profit"] = trunc(asp - true_cost, 2)
        if asp > 0:
            result["actual_margin"] = trunc((asp - true_cost) / asp, 4)
    return result


def calculate_quick(qty_on_plate, filament_used_g, filament_cost, print_time_text,
                     active_labor_min, actual_selling_price, settings):
    """Quick-estimate costing at full precision — no truncation, unlike
    calculate_pricing() above. Used by the standalone Quick Calculator page and by
    calculate_job_cost(), because job quoting works from the full-precision batch
    cost rather than the truncated catalog figure."""
    labor_rate = float(settings.get("labor_rate", 25))
    electricity_rate = float(settings.get("electricity_rate", 0.15))
    printer_watts = float(settings.get("printer_watts", 120))
    machine_wear_rate = float(settings.get("machine_wear_rate", 0.1))
    default_wholesale_margin = float(settings.get("default_wholesale_margin", 0.6))
    default_retail_margin = float(settings.get("default_retail_margin", 0.7))
    if not qty_on_plate or qty_on_plate <= 0:
        return {"error": "Qty on Plate must be greater than 0."}
    print_minutes = parse_print_time(print_time_text)
    if print_minutes is None or not math.isfinite(print_minutes) or print_minutes <= 0:
        return {"error": 'Print Time must look like "1h 50m", "2h", or "45m".'}
    filament_cost = filament_cost or 0.0
    labor_min = DEFAULT_ACTIVE_LABOR_MIN if active_labor_min in (None, "") else float(active_labor_min)
    electricity_batch = (print_minutes / 60) * (printer_watts / 1000) * electricity_rate
    machine_wear_batch = (print_minutes / 60) * machine_wear_rate
    labor_batch = (labor_min / 60) * labor_rate
    total_batch_cost = filament_cost + electricity_batch + machine_wear_batch + labor_batch
    true_cost = total_batch_cost / qty_on_plate
    result = {
        "print_minutes": print_minutes,
        "electricity_batch": electricity_batch,
        "machine_wear_batch": machine_wear_batch,
        "labor_batch": labor_batch,
        "total_batch_cost": total_batch_cost,
        "true_cost_per_item": true_cost,
        "suggested_wholesale": true_cost / (1 - default_wholesale_margin),
        "margin_40": true_cost / (1 - 0.40),
        "margin_50": true_cost / (1 - 0.50),
        "margin_60": true_cost / (1 - 0.60),
        "margin_70": true_cost / (1 - 0.70),
        "margin_80": true_cost / (1 - 0.80),
        "suggested_retail": true_cost / (1 - default_retail_margin),
        "actual_profit": None,
        "actual_margin": None,
    }
    if actual_selling_price:
        asp = float(actual_selling_price)
        result["actual_profit"] = asp - true_cost
        if asp > 0:
            result["actual_margin"] = (asp - true_cost) / asp
    return result


def calculate_part_batch(part, settings, truncating=True):
    """Costs ONE plate of ONE part. `part` is any mapping with qty_on_plate,
    filament_cost, print_time_text and active_labor_min. Returns a dict, or a dict
    with 'error' set. `truncating` selects Products-sheet behavior (truncate every
    intermediate, as calculate_pricing does) vs Quick-Calculator behavior (full precision)."""
    labor_rate       = float(settings.get("labor_rate", 25))
    electricity_rate = float(settings.get("electricity_rate", 0.15))
    printer_watts    = float(settings.get("printer_watts", 120))
    machine_wear_rate = float(settings.get("machine_wear_rate", 0.1))
    t = (lambda v, d: trunc(v, d)) if truncating else (lambda v, d: v)
    qty_on_plate = part.get("qty_on_plate")
    if not qty_on_plate or float(qty_on_plate) <= 0:
        return {"error": f"{part.get('part_name') or 'Part'}: Qty on Plate must be greater than 0."}
    qty_on_plate = float(qty_on_plate)
    print_minutes = parse_print_time(part.get("print_time_text"))
    if print_minutes is None or not math.isfinite(print_minutes) or print_minutes <= 0:
        return {"error": f'{part.get("part_name") or "Part"}: Print Time must look like "1h 50m", "2h", or "45m".'}
    filament_cost = float(part.get("filament_cost") or 0.0)
    alm = part.get("active_labor_min")
    default_alm = float(settings.get("default_active_labor_min", DEFAULT_ACTIVE_LABOR_MIN))
    labor_min = default_alm if alm in (None, "") else float(alm)
    qty_per_product = float(part.get("qty_per_product") or 1)
    if qty_per_product <= 0:
        return {"error": f"{part.get('part_name') or 'Part'}: Qty per Product must be greater than 0."}
    electricity_batch  = t((print_minutes / 60) * (printer_watts / 1000) * electricity_rate, 3)
    machine_wear_batch = t((print_minutes / 60) * machine_wear_rate, 3)
    labor_batch        = t((labor_min / 60) * labor_rate, 3)
    total_batch_cost   = t(filament_cost + electricity_batch + machine_wear_batch + labor_batch, 3)
    cost_per_part      = t(total_batch_cost / qty_on_plate, 3)
    return {
        "part_name": part.get("part_name") or "Main",
        "print_minutes": print_minutes,
        "qty_on_plate": qty_on_plate,
        "qty_per_product": qty_per_product,
        "filament_used_g": part.get("filament_used_g"),
        "filament_cost": filament_cost,
        "labor_min": labor_min,
        "electricity_batch": electricity_batch,
        "machine_wear_batch": machine_wear_batch,
        "labor_batch": labor_batch,
        "total_batch_cost": total_batch_cost,
        "cost_per_part": cost_per_part,
        "cost_contribution": cost_per_part * qty_per_product,
    }


def calculate_product_cost(parts, assembly_labor_min, actual_selling_price, settings,
                            truncating=True):
    """Costs a finished product from its bill of materials. A single-part product with
    qty_per_product=1 and no assembly labor returns exactly what the old single-plate
    calculation returned — existing costed products do not change value."""
    labor_rate = float(settings.get("labor_rate", 25))
    default_wholesale_margin = float(settings.get("default_wholesale_margin", 0.6))
    default_retail_margin    = float(settings.get("default_retail_margin", 0.7))
    scrap_rate = float(settings.get("scrap_rate", 0)) / 100.0
    if not math.isfinite(scrap_rate) or not 0 <= scrap_rate < 1:
        return {"error": "Scrap rate must be between 0 and less than 100%."}
    t = (lambda v, d: trunc(v, d)) if truncating else (lambda v, d: v)
    if not parts:
        return {"error": "This product has no parts yet — add at least one printed part."}
    costed, total_minutes, total_grams = [], 0.0, 0.0
    subtotal = 0.0
    for p in parts:
        c = calculate_part_batch(p, settings, truncating=truncating)
        if c.get("error"):
            return {"error": c["error"]}
        costed.append(c)
        subtotal += c["cost_contribution"]
        total_minutes += c["print_minutes"] * (c["qty_per_product"] / c["qty_on_plate"])
        total_grams += float(c["filament_used_g"] or 0) * (c["qty_per_product"] / c["qty_on_plate"])
    asm_min = 0.0 if assembly_labor_min in (None, "") else float(assembly_labor_min)
    assembly_cost = t((asm_min / 60) * labor_rate, 3)
    true_cost = t(subtotal + assembly_cost, 3)
    if scrap_rate > 0:
        true_cost = t(true_cost / (1 - scrap_rate), 3)
    result = {
        "parts": costed,
        "part_count": len(costed),
        "parts_subtotal": t(subtotal, 3),
        "assembly_labor_min": asm_min,
        "assembly_cost": assembly_cost,
        "scrap_rate": scrap_rate,
        "print_minutes_per_item": total_minutes,
        "filament_g_per_item": total_grams,
        "true_cost_per_item": true_cost,
        "suggested_wholesale": t(true_cost / (1 - default_wholesale_margin), 2),
        "margin_40": t(true_cost / (1 - 0.40), 2),
        "margin_50": t(true_cost / (1 - 0.50), 2),
        "margin_60": t(true_cost / (1 - 0.60), 2),
        "margin_70": t(true_cost / (1 - 0.70), 2),
        "margin_80": t(true_cost / (1 - 0.80), 2),
        "suggested_retail": t(true_cost / (1 - default_retail_margin), 2),
        "actual_profit": None,
        "actual_margin": None,
    }
    if actual_selling_price:
        asp = float(actual_selling_price)
        result["actual_profit"] = t(asp - true_cost, 2)
        if asp > 0:
            result["actual_margin"] = t((asp - true_cost) / asp, 4)
    return result


def calculate_assembly_cost(components, assembly_labor_min, actual_selling_price, settings,
                             truncating=True):
    """Costs a sellable assembly built from OTHER sellable products, one level
    only — see add_assembly_component() for where that limit is enforced.

    `components` is [{"sku", "product_name", "qty", "true_cost_per_item"}, ...].
    Each component's true_cost_per_item is looked up fresh from ITS OWN bill of
    materials by the caller (see products()) every time this runs — never
    stored here, never cached — so an assembly's cost recomputes automatically
    the moment a component's own parts change. Same margin-ladder shape as
    calculate_product_cost() on purpose, so the two render the same way."""
    labor_rate = float(settings.get("labor_rate", 25))
    default_wholesale_margin = float(settings.get("default_wholesale_margin", 0.6))
    default_retail_margin    = float(settings.get("default_retail_margin", 0.7))
    t = (lambda v, d: trunc(v, d)) if truncating else (lambda v, d: v)
    if not components:
        return {"error": "This assembly has no components yet — add at least one."}
    lines, subtotal = [], 0.0
    for c in components:
        line_cost = float(c["true_cost_per_item"]) * float(c["qty"])
        subtotal += line_cost
        lines.append({**c, "line_cost": t(line_cost, 3)})
    asm_min = 0.0 if assembly_labor_min in (None, "") else float(assembly_labor_min)
    assembly_cost = t((asm_min / 60) * labor_rate, 3)
    true_cost = t(subtotal + assembly_cost, 3)
    result = {
        "components": lines,
        "component_count": len(lines),
        "components_subtotal": t(subtotal, 3),
        "assembly_labor_min": asm_min,
        "assembly_cost": assembly_cost,
        "true_cost_per_item": true_cost,
        "suggested_wholesale": t(true_cost / (1 - default_wholesale_margin), 2),
        "margin_40": t(true_cost / (1 - 0.40), 2),
        "margin_50": t(true_cost / (1 - 0.50), 2),
        "margin_60": t(true_cost / (1 - 0.60), 2),
        "margin_70": t(true_cost / (1 - 0.70), 2),
        "margin_80": t(true_cost / (1 - 0.80), 2),
        "suggested_retail": t(true_cost / (1 - default_retail_margin), 2),
        "actual_profit": None,
        "actual_margin": None,
    }
    if actual_selling_price:
        asp = float(actual_selling_price)
        result["actual_profit"] = t(asp - true_cost, 2)
        if asp > 0:
            result["actual_margin"] = t((asp - true_cost) / asp, 4)
    return result


def _margin_ladder(cost_per_item, settings):
    """Suggested selling price at each margin target, from a per-item cost.

    Same formula the Quick Calculator has always used: price = cost / (1 - margin).
    Surfaced on the Job Calculator because quoting runs backwards — you know what
    a job costs before you know what to charge for it, and leaving Selling Price
    blank should hand you the ladder rather than a wall of zeros."""
    if not cost_per_item or cost_per_item <= 0:
        return None
    default_wholesale = float(settings.get("default_wholesale_margin", 0.6))
    default_retail = float(settings.get("default_retail_margin", 0.7))
    return {
        "margin_40": cost_per_item / (1 - 0.40),
        "margin_50": cost_per_item / (1 - 0.50),
        "margin_60": cost_per_item / (1 - 0.60),
        "margin_70": cost_per_item / (1 - 0.70),
        "margin_80": cost_per_item / (1 - 0.80),
        "suggested_wholesale": cost_per_item / (1 - default_wholesale),
        "suggested_retail": cost_per_item / (1 - default_retail),
        "wholesale_margin_pct": default_wholesale * 100,
        "retail_margin_pct": default_retail * 100,
    }


def calculate_job_cost_from_parts(order_quantity, selling_price_per_item, sales_tax_rate,
                                   other_job_cost, parts, assembly_labor_min, settings,
                                   design_minutes=0.0, is_custom_job=False):
    """Job costing for a multi-part product. Each part is planned independently — a
    holder printed 4-up and a stem printed 12-up need different numbers of print runs
    for the same order — then the runs are summed. Uses full precision (no truncation),
    consistent with calculate_quick() rather than calculate_pricing().

    Design time is deliberately NOT costed per item. It is one-time work: it happens
    once per design and never again on a reorder, so amortizing it into unit cost
    would overprice every future order of the same SKU. It is charged as its own
    line, on top of the parts, and it is excluded from true_cost_per_item,
    break_even_price and the margin ladder — those stay pure production numbers."""
    if not order_quantity or order_quantity <= 0:
        return {"error": "Customer Order Quantity must be greater than 0."}
    if not parts:
        return {"error": "This product has no parts yet — add at least one printed part."}
    labor_rate = float(settings.get("labor_rate", 25))
    scrap_rate = float(settings.get("scrap_rate", 0)) / 100.0
    if not math.isfinite(scrap_rate) or not 0 <= scrap_rate < 1:
        return {"error": "Scrap rate must be between 0 and less than 100%."}
    plan, total_production_cost, total_print_minutes = [], 0.0, 0.0
    for p in parts:
        c = calculate_part_batch(p, settings, truncating=False)
        if c.get("error"):
            return {"error": c["error"]}
        units_needed = order_quantity * c["qty_per_product"]
        runs_base = math.ceil(units_needed / c["qty_on_plate"])
        # ── Scrap: two different situations, two different mechanisms ─────────
        # On a multi-plate run, a failure allowance really is extra plates, so it
        # belongs in the run count — that is what the ceil() has always done.
        #
        # When the whole order fits on ONE plate it does not. ceil() cannot add
        # a fraction of a run, so any scrap rate above zero rounds straight up to
        # a second full plate: a 10% allowance on a one-off charges 100%, and a
        # 6-unit order of a 6-up part quotes as 12 units. You do not pre-print a
        # spare lithophane — you price the risk of having to reprint it. So on a
        # single-run job the allowance is applied as a cost uplift instead, and
        # the run count is left alone.
        #
        # Filament is deducted from the run count, not the uplift, which is
        # correct: the spare is not actually printed, so no stock moves for it.
        scrap_uplift = 1.0
        if scrap_rate > 0 and runs_base > 1:
            runs = math.ceil((units_needed / (1 - scrap_rate)) / c["qty_on_plate"])
        else:
            runs = runs_base
            if scrap_rate > 0:
                scrap_uplift = 1 / (1 - scrap_rate)
        produced = runs * c["qty_on_plate"]
        cost = runs * c["total_batch_cost"] * scrap_uplift
        total_production_cost += cost
        total_print_minutes += runs * c["print_minutes"]
        plan.append({
            "scrap_uplift": scrap_uplift,
            "part_name": c["part_name"],
            "filament_type_id": p.get("filament_type_id"),
            "filament_label": p.get("filament_label"),
            "filament_used_g": c["filament_used_g"],
            "filament_total_g": runs * float(c["filament_used_g"] or 0),
            # One entry per color, already multiplied by the run count. This is
            # what the deduction loop consumes, so a two-color part draws from
            # two pools in the right proportion.
            "filament_draws": [
                {"filament_type_id": d["filament_type_id"],
                 "filament_label": d["filament_label"],
                 "grams": runs * float(d["grams"] or 0)}
                for d in (p.get("filament_draws") or [])
            ],
            "qty_per_product": c["qty_per_product"],
            "qty_on_plate": c["qty_on_plate"],
            "units_needed": units_needed,
            "print_runs_required": runs,
            "units_produced": produced,
            "extra_units": produced - units_needed,
            "cost_per_batch": c["total_batch_cost"],
            "part_total_cost": cost,
            # cost_per_unit is cost per unit MADE, not per unit needed — it
            # only balances as "units_produced @ cost_per_unit = part_total_cost".
            # Deliberately NOT c["cost_per_part"] as-is: on a single-plate job
            # with a scrap rate, part_total_cost already carries the scrap_uplift
            # multiplier (see above) but c["cost_per_part"] does not, so that
            # would silently break the exact balance this field exists to
            # guarantee. Dividing the already-uplifted total by units produced
            # keeps the two numbers honest in every case, and is identical to
            # c["cost_per_part"] whenever scrap_uplift is 1.0 (the common case).
            "cost_per_unit": (cost / produced) if produced else 0.0,
            # What the shop actually prices against when spares don't get sold —
            # the full batch cost spread only across the units the order needed.
            "cost_per_unit_needed": (cost / units_needed) if units_needed else None,
        })
    asm_min = 0.0 if assembly_labor_min in (None, "") else float(assembly_labor_min)
    assembly_total = (asm_min / 60) * labor_rate * order_quantity
    total_production_cost += assembly_total
    other_job_cost = other_job_cost or 0.0
    total_true_job_cost = total_production_cost + other_job_cost
    selling_price_per_item = selling_price_per_item or 0.0
    sales_subtotal = order_quantity * selling_price_per_item
    sales_tax_rate = sales_tax_rate or 0.0
    # ── Design fee ────────────────────────────────────────────────────────────
    # Billed at design_rate, not labor_rate, and charged once for the whole job
    # regardless of order quantity. Kept out of total_true_job_cost so that
    # true_cost_per_item, break_even_price and the margin ladder stay production
    # numbers you can quote a reorder from without stripping anything out.
    design_rate = float(settings.get("design_rate", 50))
    design_minutes = float(design_minutes or 0)
    design_fee = (design_minutes / 60) * design_rate
    # ── Minimum job charge ────────────────────────────────────────────────────
    # Custom design jobs only. A personalized catalog product — the same cake
    # topper with different text — is a product sale, not a design job: no fee,
    # no floor. The flag is what separates them, not the price.
    min_job_charge = float(settings.get("min_job_charge", 0))
    charge_before_min = sales_subtotal + design_fee
    min_applied = bool(is_custom_job) and min_job_charge > 0 and charge_before_min < min_job_charge
    job_charge = min_job_charge if min_applied else charge_before_min
    sales_tax = job_charge * sales_tax_rate
    gross_profit = job_charge - total_true_job_cost
    return {
        "parts_plan": plan,
        "part_count": len(plan),
        "filament_total_g": sum(x["filament_total_g"] for x in plan),
        "true_cost_per_item": total_true_job_cost / order_quantity,
        "print_minutes": total_print_minutes,
        "total_print_hours": total_print_minutes / 60,
        "assembly_labor_min": asm_min,
        "assembly_total": assembly_total,
        "total_production_cost": total_production_cost,
        "other_job_cost": other_job_cost,
        "total_true_job_cost": total_true_job_cost,
        "scrap_rate": scrap_rate,
        "is_custom_job": bool(is_custom_job),
        "design_minutes": design_minutes,
        "design_rate": design_rate,
        "design_fee": design_fee,
        "min_job_charge": min_job_charge,
        "min_job_charge_applied": min_applied,
        "charge_before_min": charge_before_min,
        "job_charge": job_charge,
        "sales_subtotal": sales_subtotal,
        "sales_tax": sales_tax,
        "invoice_total": job_charge + sales_tax,
        "gross_profit": gross_profit,
        "profit_per_item": gross_profit / order_quantity,
        "actual_margin": (gross_profit / job_charge) if job_charge else None,
        "break_even_price": total_true_job_cost / order_quantity,
        "margins": _margin_ladder(total_true_job_cost / order_quantity, settings),
        "order_quantity": order_quantity,
    }


def calculate_job_cost(order_quantity, selling_price_per_item, sales_tax_rate, other_job_cost,
                        qty_on_plate, filament_used_g, filament_cost, print_time_text,
                        active_labor_min, settings):
    """Single-part job costing. Uses calculate_quick() (not calculate_pricing())
    for the batch cost, so job quotes work from untruncated figures."""
    pricing = calculate_quick(qty_on_plate, filament_used_g, filament_cost, print_time_text,
                               active_labor_min, None, settings)
    if pricing.get("error"):
        return {"error": pricing["error"]}
    if not order_quantity or order_quantity <= 0:
        return {"error": "Customer Order Quantity must be greater than 0."}
    qty_per_plate = qty_on_plate
    cost_per_batch = pricing["total_batch_cost"]
    print_runs_required = math.ceil(order_quantity / qty_per_plate)
    total_items_produced = print_runs_required * qty_per_plate
    extra_items_produced = total_items_produced - order_quantity
    total_production_cost = print_runs_required * cost_per_batch
    other_job_cost = other_job_cost or 0.0
    total_true_job_cost = total_production_cost + other_job_cost
    selling_price_per_item = selling_price_per_item or 0.0
    sales_subtotal = order_quantity * selling_price_per_item
    sales_tax_rate = sales_tax_rate or 0.0
    sales_tax = sales_subtotal * sales_tax_rate
    invoice_total = sales_subtotal + sales_tax
    gross_profit = sales_subtotal - total_true_job_cost
    profit_per_item = gross_profit / order_quantity
    actual_margin = (gross_profit / sales_subtotal) if sales_subtotal else None
    break_even_price = total_true_job_cost / order_quantity
    return {
        "true_cost_per_item": pricing["true_cost_per_item"],
        "print_minutes": pricing["print_minutes"],
        "qty_per_plate": qty_per_plate,
        "cost_per_batch": cost_per_batch,
        "print_runs_required": print_runs_required,
        "total_items_produced": total_items_produced,
        "extra_items_produced": extra_items_produced,
        "total_production_cost": total_production_cost,
        "other_job_cost": other_job_cost,
        "total_true_job_cost": total_true_job_cost,
        "sales_subtotal": sales_subtotal,
        "sales_tax": sales_tax,
        "invoice_total": invoice_total,
        "gross_profit": gross_profit,
        "profit_per_item": profit_per_item,
        "actual_margin": actual_margin,
        "break_even_price": break_even_price,
        "margins": _margin_ladder(break_even_price, settings),
        "order_quantity": order_quantity,
    }
