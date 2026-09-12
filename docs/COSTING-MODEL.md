# The costing model

This is the document most people actually want. It explains how a price comes
out of this tool and, more usefully, *why* each decision was made — including
several that look wrong until you have quoted a real job and been burned.

You can read this without running the software. If you take nothing else, take
the four rules at the bottom.

---

## 1. Everything starts at the plate, not the item

The unit of production is a **plate**, not a part. You do not print one bracket;
you print a plate with six brackets on it, and that plate costs what it costs
whether you needed six or four.

So every part in the catalog records:

- `qty_on_plate` — how many come off one plate
- `print_time_text` — how long that plate takes (`"1h 50m"`), typed from your slicer
- `filament_cost` — the filament that plate consumes, in money
- `active_labor_min` — hands-on minutes per plate: harvesting, deburring, bagging
- `qty_per_product` — how many of this part go into one finished product

A plate costs:

```
electricity  = (print_minutes / 60) x (printer_watts / 1000) x electricity_rate
machine_wear = (print_minutes / 60) x machine_wear_rate
labour       = (active_labor_min / 60) x labor_rate
plate_total  = filament_cost + electricity + machine_wear + labour
unit_cost    = plate_total / qty_on_plate
```

`machine_wear_rate` is a dollars-per-hour sinking fund for nozzles, belts, build
plates and the eventual printer replacement. Default is $0.10/hr. It is not
depreciation in an accounting sense; it is money you will spend and would
otherwise forget to charge for.

**`active_labor_min` is per plate, not per item, and it recurs on every single
run forever.** This is the field people get wrong. Putting one-time work here
overprices every future reorder of that SKU permanently. One-time work is design
time — see section 5.

---

## 2. Two precision modes, kept on purpose

There are two costing paths and they do not agree to the penny:

| Function | Behaviour | Used by |
|---|---|---|
| `calculate_pricing()` | Truncates every intermediate to 3 decimals | Catalog / Products page |
| `calculate_quick()` | Full floating-point precision | Quick Calculator, all job quoting |

This is inherited from the spreadsheet lineage this tool replaced, where the
catalog sheet and the quoting sheet rounded differently and therefore never
matched exactly. Rather than quietly unify them and change everyone's catalog
prices, both behaviours are preserved.

Practical consequence: **a job quote and a catalog price for the same item can
differ by a cent or two. That is expected, not a bug.** Quote from the job
calculator.

---

## 3. Filament cost is pooled across brands, stock is not

If you buy PLA Black from two vendors at different prices, you have:

- **One cost pool.** `cost_per_gram_pooled()` returns the grams-weighted average
  purchase price across every brand sharing that material and colour. Two 1,000 g
  spools at $17.90 and $24.99 give $0.021445/g.
- **Two stock ledgers.** Brand rows stay separate, because reordering is a
  per-brand decision and you need to know which one ran out.

When a job consumes filament, grams are allocated across the brand ledgers that
actually have stock, so inventory stays truthful while the *price* stays pooled.

Matching normalizes case and whitespace. It does **not** merge genuine finish
differences — put `PLA Matte` and `PLA` in the material field separately if you
want them priced separately, because they cost different money.

There is also a per-part override: `filament_cost_override` is an explicit
boolean flag, not an inference from a blank field. Set it and your hand-typed
figure wins while the derived figure is still displayed next to it. Precedence is
always recorded, never guessed.

---

## 4. Scrap: the part that is genuinely subtle

A scrap rate is a failure allowance. The naive implementation is to inflate the
required quantity and let the run count round up. That is correct on a big job
and catastrophically wrong on a small one.

Consider a 10% scrap rate on a 6-up part, ordering exactly 6:

```
units_needed / (1 - 0.10) = 6.67  ->  ceil(6.67 / 6) = 2 plates
```

A 10% allowance just charged the customer **100% more**. You did not print a
spare. You would not print a spare. You priced the *risk* of having to reprint.

So the model branches:

- **Multi-plate jobs** (`runs_base > 1`): the allowance is real extra plates, and
  goes into the run count. `scrap_uplift = 1.0`.
- **Single-plate jobs**: the run count is untouched and the allowance is applied
  as a cost multiplier instead: `scrap_uplift = 1 / (1 - scrap_rate)`.

Filament stock is always deducted from the **run count**, never the uplift —
because on a single-plate job the spare is not physically printed, so no grams
should move.

### The consequence that trips people up

On a single-plate job with scrap, `part_total_cost` carries the uplift and
`cost_per_part` does not. So:

> **Per-unit cost is `part_total_cost / units_produced`. It is never
> `cost_per_part`.**

Using the latter produces a line item where `units × unit_cost ≠ total`, and a
customer will notice. The two are identical whenever `scrap_uplift` is 1.0, which
is why the bug hides until someone quotes a one-off.

Two per-unit figures are reported, and they are different on purpose:

- `cost_per_unit` = `part_total_cost / units_produced` — what each unit made cost
- `cost_per_unit_needed` = `part_total_cost / units_needed` — what each *delivered*
  unit cost, carrying the spares

Quote from the second one when spares will not be sold.

---

## 5. Design time is not labour, and it is never per-item

Two different rates exist and conflating them is the single most expensive
mistake in this model:

| | `labor_rate` (default $25/hr) | `design_rate` (default $50/hr) |
|---|---|---|
| What it pays for | Machine tending — harvesting, deburring, bagging | CAD, setup, one-time engineering |
| How often | Every plate, every run, forever | Once per design, never again |
| Where it lives | `active_labor_min` on a part | `design_minutes` on a job |

The design fee is charged **once for the whole job, regardless of quantity**, and
it is deliberately excluded from `true_cost_per_item`, `break_even_price` and the
margin ladder. Those stay pure production numbers, so you can quote a reorder
straight off them without stripping anything out.

Amortising design into unit cost overprices every future order of that SKU
permanently. Do not do it.

---

## 6. Minimum job charge applies to custom work only

`min_job_charge` (default $40) is a floor, and it applies **only when
`is_custom_job` is set**.

A personalised catalog product is not a custom job. A cake topper with a
different name on it is a SKU with a text field — it gets no design fee and no
minimum. What separates the two is the flag, not the price.

```
charge_before_min = sales_subtotal + design_fee
job_charge        = min_job_charge   if is_custom_job and charge_before_min < min_job_charge
                    charge_before_min otherwise
sales_tax         = job_charge x sales_tax_rate
invoice_total     = job_charge + sales_tax
```

Tax is applied after the minimum, not before.

---

## 7. Margin is margin, not markup

The margin ladder divides; it does not multiply:

```
price = cost / (1 - margin)
```

A 60% margin on a $10 cost is $25.00, not $16.00. The ladder shows 40/50/60/70/80%
so you can see what you are giving up when you discount. `default_wholesale_margin`
(0.6) and `default_retail_margin` (0.7) pick the two highlighted figures.

---

## 8. Multi-part products plan each part separately

A product made of a holder printed 4-up and a stem printed 12-up needs different
numbers of runs for the same order quantity. Each part is planned independently,
then summed. Assembly labour is applied per finished unit, not per plate.

Assemblies nest exactly **one level**. A component may not itself be an assembly.
Self-reference and both nesting directions are rejected in application logic,
because a foreign key cannot detect a cycle. This is a scope decision, not a
missing feature.

Assembly cost is looked up fresh on every render and never cached on the assembly
row, so a change to a component — or to a spool price — propagates immediately.

---

## 9. Saved jobs are frozen, permanently

A saved job stores its complete inputs, the parts snapshot and the computed
result in `payload_json`. Job history reads that JSON directly and **never
recomputes**.

This is the most important guarantee in the system. A quote you gave in March
still shows March's numbers in December, after you have changed your labour rate
twice and filament prices have moved. If it re-priced itself, your records would
silently become fiction and you would have no way to explain an old invoice.

There is no recompute path. Do not add one.

---

## The four rules

1. **Plate figures, never per-item figures.** `qty_on_plate` is how many come off
   one plate; `qty_per_product` is how many go into one finished item. They are
   not the same number and swapping them corrupts every downstream price.
2. **Per-unit cost is `part_total_cost / units_produced`.** Never `cost_per_part`.
3. **One-time work is `design_minutes`, recurring work is `active_labor_min`.**
   Mixing them overprices reorders forever.
4. **Saved jobs never re-price.** Changing a Settings rate re-prices the live
   catalog instantly and touches no saved quote.
