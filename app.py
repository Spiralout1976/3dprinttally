from flask import Flask, render_template, render_template_string, request, redirect, url_for, flash, send_file, jsonify, Response
import sqlite3, csv, io, json, secrets, os, math, re
from pathlib import Path
from io import BytesIO
from datetime import datetime
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.graphics.barcode import qr
from reportlab.graphics.shapes import Drawing
from reportlab.graphics import renderPDF
from config import *
ALLOWED_PHOTO_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
# SKU shape: TYPE-NNN (e.g. SGN-003, BASE-001). Enforced on create and rename so a
# typo like SGl-001 can't quietly found a permanent phantom type prefix.
#
# Department was dropped from the SKU string on purpose — see products.department
# below. A physical part (a base shared by five departments' products) has ONE
# identity regardless of who uses it; baking a department into the identifier
# forced a duplicate SKU per department for the exact same object. Department
# is now a real, queryable column on the product that sells it, not a segment
# of the string that names it.
SKU_RE = re.compile(r"^[A-Z0-9]{2,10}-\d{3,}$")
_SKU_TYPE_RE = re.compile(r"^([A-Z0-9]{2,10})-\d{3,}$")
def sku_type_segment(sku):
    """The TYPE in TYPE-NNN, for grouping the Job Calculator's part pickers
    only — display, not identity. No new field: the type is already the
    first segment of the SKU string SKU_RE already requires. A SKU that
    doesn't match that shape (shouldn't happen given SKU_RE, but a picker
    must never hide a part over a formatting surprise) groups under "Other"
    rather than being dropped."""
    m = _SKU_TYPE_RE.match((sku or "").strip().upper())
    return m.group(1) if m else "Other"
app = Flask(__name__)
from security import configure_security
configure_security(app)
from database import db
from schema import init_db
from costing import *
from costing import _margin_ladder
from pdf_output import *
def get_settings():
    con = db()
    rows = con.execute("SELECT key, value FROM settings").fetchall()
    con.close()
    values = dict(DEFAULT_SETTINGS)
    values.update({r["key"]: r["value"] for r in rows})
    return values
from photos import save_product_photo

def _part_filament_draws(part):
    """Every filament draw a product_part carries, oldest data-model draw
    first. filament_draws_json, when present, is the complete authoritative
    list (including what would otherwise be "draw 0"). When it's empty/NULL —
    true for the overwhelming majority of parts, which have one color — draw
    0 is synthesized from the plain filament_type_id/filament_used_g columns,
    the only ones a real FOREIGN KEY can actually constrain."""
    extra = part.get("filament_draws_json")
    if extra:
        try:
            draws = json.loads(extra)
            if draws:
                return draws
        except (ValueError, TypeError):
            pass
    fid = part.get("filament_type_id")
    if fid:
        return [{"filament_type_id": fid, "grams": part.get("filament_used_g")}]
    return []
def resolve_filament_derivation(con, part):
    """(derived_cost, pool_label, total_grams) for one product_part's linked
    filament, priced against the MATERIAL+COLOR blend across every brand
    (filament.pooled_cost_for_type) — never a single brand's own rate, per
    the requirement that the Products-page estimate blend brands of the
    same color. derived_cost is None when nothing is linked, or when every
    linked color has never actually been purchased — the caller falls back
    to the hand-typed figure exactly as it always has for an unlinked part."""
    draws = _part_filament_draws(part)
    if not draws:
        return None, None, None
    from filament import pooled_cost_for_type, pool_label
    total_cost, total_g, priced_any, labels = 0.0, 0.0, False, []
    for d in draws:
        fid, grams = d.get("filament_type_id"), d.get("grams")
        if not fid or not grams:
            continue
        row = con.execute(
            "SELECT material, color_name, brand FROM filament_types WHERE id=?",
            (fid,)).fetchone()
        if not row:
            continue    # linked color has since been deleted — price the rest
        labels.append(pool_label(row))
        total_g += grams
        cpg = pooled_cost_for_type(con, fid)
        if cpg is not None:
            total_cost += grams * cpg
            priced_any = True
    label = ", ".join(labels) if labels else None
    return (total_cost if priced_any else None), label, (total_g or None)
def get_product_parts(con, sku):
    """Parts for a SKU. A SKU is still the recipe, not the color — SGN-001 is
    the same part whether a customer wants it white or blue, and the Job
    Calculator is still where that choice is actually made per job. But a
    part MAY optionally link a filament pool for its Products-page ESTIMATE
    (filament_type_id, plus filament_draws_json for a second color) — when
    it does, filament_cost below is overwritten with the live pooled-cost
    derivation rather than the raw hand-typed column, unless
    filament_cost_override is set. filament_typed_cost always preserves the
    original hand-entered figure regardless of which value filament_cost
    ends up holding, so the UI can show what's underneath an override or a
    derivation."""
    rows = con.execute(
        "SELECT * FROM product_parts WHERE sku=? ORDER BY sort_order, id", (sku,)
    ).fetchall()
    parts = [dict(r) for r in rows]
    for p in parts:
        p["filament_typed_cost"] = p.get("filament_cost")
        if not p.get("filament_type_id"):
            p["filament_derived"] = False
            p["filament_overridden"] = False
            continue
        derived_cost, pool_label, total_g = resolve_filament_derivation(con, p)
        p["filament_pool_label"] = pool_label
        p["filament_derived_cost"] = derived_cost
        if total_g:
            p["filament_used_g"] = total_g
        if p.get("filament_cost_override"):
            p["filament_derived"] = False
            p["filament_overridden"] = True
        elif derived_cost is not None:
            p["filament_cost"] = derived_cost
            p["filament_derived"] = True
            p["filament_overridden"] = False
        else:
            # Linked, but nothing costed for that color across any brand yet —
            # falls back to the hand-typed figure, same as an unlinked part.
            p["filament_derived"] = False
            p["filament_overridden"] = False
    return parts
def get_assembly_components(con, sku):
    """The components one assembly SKU is built from, if any. A product with
    rows here is an assembly; a product with none is either a plain printed
    product (has product_parts) or a component itself (has neither, and may
    be referenced by some OTHER assembly's rows)."""
    rows = con.execute(
        "SELECT * FROM assembly_components WHERE assembly_sku=? ORDER BY sort_order, id",
        (sku,)
    ).fetchall()
    return [dict(r) for r in rows]
def _color_draws(form, pid, default_grams, picked_pool, prefix=""):
    """Which filament(s) one printed part draws from, and how many grams of each.

    A part is usually one color, but an AMS print can lay two or more inside a
    SINGLE printed piece — a sign face in blue with red lettering is one part and
    two filaments. That is a different thing from a two-PART product where each
    part is its own color, and the schema has to express both.

    Fields are `{prefix}filp_{pid}_{n}` (pool) and `{prefix}filg_{pid}_{n}` (grams),
    n starting at 0. For a single color the grams box is hidden in the UI and
    left blank, so slot 0 falls back to the plate figure the part already carries
    — meaning nothing changes for the common case.

    Returns (draws, total_grams, total_cost). total_cost is None when no picked
    pool has a price, so the caller falls back to the hand-entered figure."""
    idx = set()
    for k in form.keys():
        if k.startswith(f"{prefix}filp_{pid}_"):
            tail = k[len(f"{prefix}filp_{pid}_"):]
            if tail.isdigit():
                idx.add(int(tail))
    if not idx:
        return [], None, None

    def g(n):
        v = (form.get(f"{prefix}filg_{pid}_{n}") or "").strip()
        if v == "":
            return None
        try:
            return float(v)
        except ValueError:
            return None

    multi = len(idx) > 1
    draws, total_g, total_cost, priced_any = [], 0.0, 0.0, False
    for n in sorted(idx):
        fid, cpg, label = picked_pool(f"{prefix}filp_{pid}_{n}")
        grams = g(n)
        if grams is None and not multi:
            grams = default_grams          # single color: use the plate figure
        if not fid or not grams or grams <= 0:
            continue
        draws.append({"filament_type_id": fid, "filament_label": label,
                      "grams": grams})
        total_g += grams
        if cpg is not None:
            total_cost += grams * cpg
            priced_any = True
    if not draws:
        return [], None, None
    return draws, total_g, (total_cost if priced_any else None)




def _custom_parts_from_form(form, picked_pool):
    """Build the same part dicts the catalog produces, but from typed-in rows.

    Custom job rows are named cp_<field>_<n>. The row index is arbitrary and
    non-contiguous — the browser adds and removes rows freely — so indices are
    discovered from the submitted keys rather than assumed to run 0..n. A row with
    no name and no plate quantity is treated as an empty row the user left behind
    and skipped silently."""
    idx = set()
    for k in form.keys():
        if k.startswith("cp_name_"):
            tail = k[len("cp_name_"):]
            if tail.isdigit():
                idx.add(int(tail))

    def num(key, i):
        v = (form.get(f"{key}_{i}") or "").strip()
        if v == "":
            return None
        try:
            return float(v)
        except ValueError:
            return None

    parts = []
    for i in sorted(idx):
        name = (form.get(f"cp_name_{i}") or "").strip()
        plate = num("cp_plate", i)
        if not name and not plate:
            continue
        grams = num("cp_grams", i)
        cost = num("cp_cost", i)
        draws, tot_g, tot_cost = _color_draws(form, i, grams, picked_pool,
                                               prefix="cp_")
        fid = draws[0]["filament_type_id"] if draws else None
        label = ", ".join(d["filament_label"] or "?" for d in draws) if draws else None
        if draws and len(draws) > 1:
            grams = tot_g
        if draws and tot_cost is not None:
            cost = tot_cost
        parts.append({
            "id": f"c{i}",
            "part_name": name or f"Part {i + 1}",
            # Which catalog SKU this row was loaded from, if any — blank for a
            # row typed by hand. Recorded so payload_json remembers where
            # each line actually came from (see /api/job-parts/<sku>), even
            # though qty_on_plate below is always this job's own number, never
            # written back to that SKU.
            "source_sku": (form.get(f"cp_sku_{i}") or "").strip().upper(),
            "qty_per_product": num("cp_qty_per", i) or 1,
            "qty_on_plate": plate,
            "filament_used_g": grams,
            "filament_cost": cost,
            "print_time_text": (form.get(f"cp_time_{i}") or "").strip(),
            "active_labor_min": num("cp_labor", i),
            "filament_type_id": fid,
            "filament_label": label,
            "filament_draws": draws,
        })
    return parts


def _part_to_job_row(p, source_sku, qty_multiplier=1):
    """One product_parts row, reshaped into a job row — the same shape
    addCustomRow()/the color-stack JS expects for a freehand row, just
    pre-filled from the catalog instead of typed. qty_on_plate is copied as a
    STARTING DEFAULT only; the job route never writes it back to
    product_parts, and job_calculator.html leaves it a normal editable
    input, same as every other field here.

    print_time_text is deliberately NOT pre-filled. The catalog's own figure
    is for that part printed alone; the real time for THIS run depends on
    everything actually sharing the plate, which only Bambu Studio's slicer
    for this specific job knows. It is typed fresh every time, and nothing
    here ever overwrites what he's already typed."""
    return {
        "part_name": p.get("part_name") or "Part",
        "source_sku": source_sku,
        "qty_per_product": float(p.get("qty_per_product") or 1) * qty_multiplier,
        "qty_on_plate": p.get("qty_on_plate"),
        "print_time_text": "",
        "active_labor_min": p.get("active_labor_min"),
        "filament_used_g": p.get("filament_used_g"),
        "filament_cost": p.get("filament_cost"),
        "filament_type_id": p.get("filament_type_id"),
        "filament_draws": _part_filament_draws(p),
    }
def _job_rows_for_sku(con, sku):
    """Every printed-part row one catalog SKU contributes to a job, read-only,
    in the shape _part_to_job_row() above returns.

    A plain product returns its own product_parts, one row each. An assembly
    (has assembly_components) returns its COMPONENTS' own product_parts
    instead — one level of nesting, the same rule add_assembly_component()
    enforces when the assembly is built — with each row's qty_per_product
    multiplied by how many of that component the assembly needs, so ordering
    N assemblies scales every underlying part correctly.

    Never touches product_parts or assembly_components. Returns
    (rows, product_name, is_assembly, error)."""
    sku = (sku or "").strip().upper()
    prow = con.execute("SELECT product_name FROM products WHERE sku=?", (sku,)).fetchone()
    if not prow:
        return [], "", False, f"SKU not found: {sku}"
    components = get_assembly_components(con, sku)
    rows = []
    if components:
        for ac in components:
            cparts = get_product_parts(con, ac["component_sku"])
            for p in cparts:
                rows.append(_part_to_job_row(p, ac["component_sku"],
                                             qty_multiplier=ac["qty"]))
    else:
        for p in get_product_parts(con, sku):
            rows.append(_part_to_job_row(p, sku, qty_multiplier=1))
    return rows, prow["product_name"], bool(components), None
def open_design_session(con):
    """The one session whose clock is running, or None.

    Deliberately one at a time. Two clocks running at once is how you end up
    billing the same hour twice, and there is exactly one of you."""
    row = con.execute("""SELECT * FROM design_sessions WHERE ended_at IS NULL
                         ORDER BY id DESC LIMIT 1""").fetchone()
    return dict(row) if row else None


def unbilled_design_minutes(con, sku="", customer=""):
    """Billable, finished, not-yet-attached design time for a SKU or customer.

    Rows carry billed_job_id once they have been charged on a saved quote, so the
    same three hours can never be billed twice — which is the whole reason the
    fee lives in a table instead of a number you retype into a box."""
    where = ["ended_at IS NOT NULL", "billable=1", "billed_job_id IS NULL"]
    args = []
    if sku:
        where.append("sku=?")
        args.append(sku)
    elif customer:
        where.append("customer=?")
        args.append(customer)
    else:
        return 0.0, []
    sql = "SELECT * FROM design_sessions WHERE " + " AND ".join(where) + " ORDER BY id"
    rows = [dict(r) for r in con.execute(sql, args).fetchall()]
    return sum(float(r["minutes"] or 0) for r in rows), rows


# Initialize and migrate once when the application process starts.
init_db()
@app.context_processor
def inject_design_clock():
    """Makes the running clock available to base.html on EVERY page.

    Without this the banner would only appear on /design — which is precisely
    the page you are not on when you wander off and leave the clock running for
    eleven hours. Wrapped because a database that has not been through init_db()
    yet has no design_sessions table, and a missing banner must never take the
    whole app down."""
    try:
        con = db()
        running = open_design_session(con)
        con.close()
    except Exception:
        running = None
    return {"design_clock": running}


@app.before_request
def ensure_db():
    # Handles the rare case where the bind-mounted DB is removed while running.
    if not DB_PATH.exists():
        init_db()
def costing_readiness():
    """Is this install configured enough to quote a real price?

    Two independent ways a fresh install produces a confident wrong number:
    the cost rates are still the shipped examples, or there is no filament at
    all — in which case every product's filament cost is zero and the largest
    single input to a price is silently missing. Both are warnings, never
    blocks: an empty shop is allowed to experiment.
    """
    from config import DEFAULT_SETTINGS
    settings = get_settings()
    watched = ("labor_rate", "electricity_rate", "printer_watts", "machine_wear_rate")
    rates_untouched = all(
        str(settings.get(k, "")) == str(DEFAULT_SETTINGS[k]) for k in watched
    )
    con = db()
    filament_count = con.execute("SELECT COUNT(*) FROM filament_types").fetchone()[0]
    product_count = con.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    job_count = con.execute("SELECT COUNT(*) FROM job_costs").fetchone()[0]
    con.close()
    return {
        "rates_untouched": rates_untouched,
        "no_filament": filament_count == 0,
        "first_run": product_count == 0 and filament_count == 0 and job_count == 0,
    }
@app.route("/")
def dashboard():
    # A brand-new install has nothing to show: a dashboard of zeros tells a
    # first-time user nothing about what this is or where to start. Send them
    # to the guide until they have actually put something in.
    if costing_readiness()["first_run"]:
        return redirect(url_for("guide"))
    con = db()
    stats = {
        "products": con.execute("SELECT COUNT(*) FROM products WHERE active=1").fetchone()[0],
        "jobs": con.execute("SELECT COUNT(*) FROM print_jobs").fetchone()[0],
        "labels": con.execute("SELECT COALESCE(SUM(total_labels),0) FROM print_jobs").fetchone()[0],
    }
    recent = con.execute("SELECT * FROM print_jobs ORDER BY id DESC LIMIT 5").fetchall()
    con.close()
    return render_template("dashboard.html", stats=stats, recent=recent)
@app.route("/labels")
def labels():
    con = db()
    products = con.execute("""
        SELECT sku, product_name, material
        FROM products WHERE active=1 ORDER BY product_name
    """).fetchall()
    con.close()
    return render_template("labels.html", products=products)
@app.post("/generate")
def generate():
    skus = request.form.getlist("sku[]")
    qtys = request.form.getlist("qty[]")
    copies = request.form.getlist("copies[]")
    start_position = max(1, min(30, int(request.form.get("start_position", "1") or "1")))
    from flask import current_app, abort
    max_labels = current_app.config["MAX_LABELS"]
    con = db()
    labels_out = []
    payload = []
    for sku, qty, copy_count in zip(skus, qtys, copies):
        sku = sku.strip().upper()
        qty = qty.strip()
        if not sku or not qty:
            continue
        row = con.execute("""
            SELECT sku, product_name, department FROM products WHERE sku=? AND active=1
        """, (sku,)).fetchone()
        if not row:
            con.close()
            flash(f"SKU not found: {sku}", "error")
            return redirect(url_for("labels"))
        try:
            n = max(1, int(copy_count or "1"))
        except ValueError:
            n = 1
        if len(labels_out) + n > max_labels:
            con.close()
            abort(400, description=f"A sheet job may contain at most {max_labels} labels.")
        payload.append({"sku": row["sku"], "product_name": row["product_name"],
                        "department": row["department"], "qty": qty, "copies": n})
        for _ in range(n):
            labels_out.append({"sku": row["sku"], "product_name": row["product_name"],
                               "department": row["department"], "qty": qty})
    if not labels_out:
        con.close()
        flash("Add at least one label.", "error")
        return redirect(url_for("labels"))
    settings = get_settings()
    created_at = datetime.now().isoformat(timespec="seconds")
    con.execute("""
        INSERT INTO print_jobs (created_at, total_labels, start_position, payload_json)
        VALUES (?, ?, ?, ?)
    """, (created_at, len(labels_out), start_position, json.dumps(payload)))
    con.commit()
    con.close()
    pdf = make_pdf(labels_out, start_position, settings)
    return send_file(pdf, mimetype="application/pdf", as_attachment=True, download_name="labels-5160.pdf")
GUIDE_TEMPLATE = """{% extends "base.html" %}
{% block title %}Product Guide · 3DPrintTally{% endblock %}
{% block page_title %}Product Guide{% endblock %}
{% block page_subtitle %}How to build a product, cost it, and get it ready for a customer job.{% endblock %}
{% block content %}

{% if first_run %}
<section class="card" style="border-left:3px solid var(--accent, #c0392b);">
  <div class="card-head"><h2>Start here</h2></div>
  <p>This install is empty. Work through these in order &mdash; each step depends on
     the one before it, so skipping ahead produces prices that look right and are not.</p>
  <ol class="steps">
    <li><strong>Set your rates.</strong> The cost settings are pre-filled with
        <em>example</em> figures, not measurements. Your electricity price, your
        printer's draw and your own hourly rate are all different.
        <a href="{{ url_for('settings') }}#cost">Open cost &amp; pricing settings</a>
        and set labor rate, electricity rate, printer wattage and sales tax.</li>
    <li><strong>Add your filament.</strong> <a href="{{ url_for('filament.index') }}">Filament</a>
        &mdash; the spools you own and what you paid. Until something is here,
        every filament cost is zero.</li>
    <li><strong>Create a product.</strong> <a href="{{ url_for('products') }}">Products</a>
        &mdash; a SKU, then the printed parts it is made of.</li>
    <li><strong>Quote a job.</strong> <a href="{{ url_for('job_calculator') }}">Job Calculator</a>
        &mdash; pick products, enter print time from your slicer, get a price.</li>
  </ol>
  <p class="field-note">Once you have added anything at all, the Dashboard becomes
     your home page and this guide stays available from the sidebar.</p>
</section>
{% endif %}

<style>
  .guide { max-width: 60rem; }
  .guide h3 { margin: 0 0 6px; }
  .guide ol, .guide ul { margin: 8px 0 0; padding-left: 1.25rem; }
  .guide li { margin-bottom: 7px; line-height: 1.5; }
  .guide p { line-height: 1.55; }
  .guide code { padding: 1px 5px; border-radius: 4px; background: rgba(127,127,127,0.14); font-size: 0.92em; }
  .guide .step-num {
    display: inline-flex; align-items: center; justify-content: center;
    width: 1.6rem; height: 1.6rem; border-radius: 50%; margin-right: 8px;
    background: var(--accent, #c0392b); color: #fff; font-size: 0.85rem; font-weight: 700;
  }
  .guide .callout {
    border-left: 3px solid var(--accent, #c0392b);
    padding: 10px 14px; margin: 12px 0 0;
    background: rgba(127,127,127,0.07); border-radius: 0 6px 6px 0;
  }
  .guide .callout strong { display: block; margin-bottom: 3px; }
  .guide .toc a { display: block; padding: 3px 0; }
  .guide dl { margin: 8px 0 0; }
  .guide dt { font-weight: 700; margin-top: 9px; }
  .guide dd { margin: 2px 0 0 1.1rem; }
</style>

<div class="guide">

<section class="card">
  <div class="card-head"><h2>The short version</h2></div>
  <p>
    A <strong>product</strong> is a finished thing you sell. It is made of one or more
    <strong>printed parts</strong>. Cost is calculated per part and added up, so a sign holder
    that is a body plus a stem is one product with two parts — not two products.
  </p>
  <p style="margin-top:8px">
    Everything downstream depends on this. Labels, the job calculator and job history all read
    from the product record, so time spent getting a product right is spent once.
  </p>
  <div class="callout">
    <strong>The one-line workflow</strong>
    Design → test print → slice in Bambu Studio → create the product here with those numbers →
    add any extra parts → set your selling price → cost the customer order on the Job Calculator.
  </div>
</section>

<section class="card">
  <div class="card-head"><h2>Before your first product</h2></div>
  <p>Set these once on the <a href="{{ url_for('settings') }}">Settings</a> page. Every cost in the system is built on them.</p>
  <dl>
    <dt>Labor Rate ($/hour)</dt>
    <dd>What your time is worth. Applied to both per-part active labor and assembly labor.</dd>
    <dt>Electricity Rate ($/kWh) and Average Printer Draw (watts)</dt>
    <dd>Together these turn print time into an electricity cost.</dd>
    <dt>Machine Wear ($/print hour)</dt>
    <dd>Your printer wearing itself out. Nozzles, belts, eventual replacement.</dd>
    <dt>Default Active Labor (min per plate)</dt>
    <dd>Used for any part where you leave Active Labor blank. This is plate handling — clearing the bed, starting the next run.</dd>
    <dt>Scrap / Failure Rate (%)</dt>
    <dd>Share of prints you expect to fail. At <code>0</code>, every cost assumes nothing ever fails. Set a real number and both product cost and job print runs are grossed up to cover the losses.</dd>
    <dt>Sales Tax Rate (%)</dt>
    <dd>Pre-fills on the Job Calculator so you stop retyping it.</dd>
    <dt>Default Wholesale / Retail Margin</dt>
    <dd>Drives the Suggested Wholesale and Suggested Retail figures.</dd>
  </dl>
  <div class="callout">
    <strong>Changing a rate re-prices everything</strong>
    Costs are calculated live from these settings, never frozen into the product. Raise your labor
    rate and every product in the catalog re-costs immediately. Jobs you already saved keep the
    numbers you quoted.
  </div>
</section>

<section class="card">
  <div class="card-head"><h2><span class="step-num">1</span>Create the product</h2></div>
  <p>On the <a href="{{ url_for('products') }}">Products</a> page, use the <strong>Add Product</strong> card at the top left.</p>
  <ol>
    <li>
      <strong>Item Type</strong> — what it is: <code>SGN</code>, <code>HOOK</code>, <code>CLIP</code>,
      <code>FRAME</code>, <code>DISP</code>, <code>PROT</code>, or a new one you invent. This is what
      drives the SKU.
    </li>
    <li>
      <strong>Generated SKU</strong> fills itself in as <code>TYPE-NNN</code> — for example
      <code>SGN-003</code> — using the next free number for that item type. It is read-only here
      on purpose. Hit <strong>↻ Refresh</strong> if you have had the form open a while and want to
      re-check the number.
    </li>
    <li>
      <strong>Department</strong> — who sells it: <code>Meat</code>, <code>Bakery</code>,
      <code>Seafood</code>, and so on, typed or picked from the list. This is <em>not</em> part of
      the SKU any more — a base shared by five departments is one physical object with one SKU,
      and department is just a label on whichever product actually sells it. Leave it blank for a
      shared part no single department owns; a blank department shows nothing, never a placeholder.
    </li>
    <li><strong>Product Name</strong> — the human-readable name. This is what you will recognize in a dropdown six months from now, so "Deli Salad Case Sign Holder" beats "Sign v2".</li>
    <li><strong>Material</strong> — PETG, PLA, and so on.</li>
    <li><strong>Collection</strong> — choose Office Organization, Kitchen Organization, Bathroom Organization, or type your own. Saved names become reusable choices. Filter the catalog by collection; moving a product keeps its SKU.</li>
    <li><strong>Notes</strong> — anything you want to remember about it.</li>
    <li>
      <strong>First Printed Part</strong> — the Bambu Studio numbers for one full plate. Fill these
      in and the product is costed the moment you create it:
      <ul style="margin-top:6px">
        <li><strong>Qty on Plate</strong> — how many come off one plate</li>
        <li><strong>Filament Used — Plate (g)</strong> — grams for the whole plate</li>
        <li><strong>Filament Cost — Plate ($)</strong> — the dollar figure Bambu Studio gives you for the whole plate</li>
        <li><strong>Print Time</strong> — <code>1h 50m</code>, <code>2h</code>, or <code>45m</code></li>
        <li><strong>Active Labor</strong> — leave blank to use the Settings default</li>
      </ul>
    </li>
    <li>Click <strong>Add Product</strong>.</li>
  </ol>
  <div class="callout">
    <strong>Every value is for the whole plate, never per item</strong>
    If four holders come off one plate using 55.79 g at $1.67, enter 4, 55.79 and 1.67. The system
    divides by Qty on Plate to get cost per item. Entering per-item figures will quietly quadruple
    your costs.
  </div>
  <div class="callout">
    <strong>If the SKU is already taken</strong>
    You get an error and <em>everything you typed stays on the form</em>. Change the SKU and submit
    again — you will not lose the entry.
  </div>
</section>

<section class="card">
  <div class="card-head"><h2><span class="step-num">2</span>Add the other parts</h2></div>
  <p>Skip this if your product is a single piece. Otherwise, find the product in the Catalog and click <strong>Edit</strong>.</p>
  <ol>
    <li>Scroll to <strong>Printed Parts</strong>. The part you created in step 1 is there, named "Main". Rename it to something meaningful — "Body", "Holder", "Base".</li>
    <li>Use the dashed <strong>+ Add Part</strong> form below it for the next piece.</li>
    <li>
      <strong>Qty per Product</strong> is the one that catches people out. It is how many of
      <em>this part</em> go into <em>one finished product</em>. A stem is 1. A display needing four
      feet is 4.
    </li>
    <li>Fill in that part's own plate numbers — it has its own Qty on Plate, print time and filament, because it prints separately.</li>
    <li>Click <strong>+ Add Part</strong>. Repeat for every piece.</li>
  </ol>
  <p style="margin-top:10px">Each part can also carry its own <strong>Nozzle Size</strong>, <strong>Layer Height</strong>, <strong>STL Filename</strong> and <strong>3MF Filename</strong> — worth filling in so you can find the right file later. Name print files after the SKU; the filename boxes show a suggested name in gray as a reminder.</p>
  <div class="callout">
    <strong>Parts print at different rates, and the maths respects that</strong>
    A body 4-up and a stem 12-up need different numbers of print runs for the same order. The Job
    Calculator plans each part separately and adds them together, rather than assuming everything
    comes off one plate.
  </div>
  <p style="margin-top:10px">A product must keep at least one part — the Remove Part button only appears when there are two or more.</p>
</section>

<section class="card">
  <div class="card-head"><h2><span class="step-num">3</span>Assembly labor and selling price</h2></div>
  <p>Still in <strong>Edit</strong>, in the <strong>Assembly &amp; Price</strong> section:</p>
  <ul>
    <li><strong>Assembly Labor (min per finished item)</strong> — time to put the parts together, glue, insert magnets, clean up. Charged once per finished item, on top of each part's own labor. Leave blank for a single-piece product.</li>
    <li><strong>Actual Selling Price ($)</strong> — what you actually charge. This unlocks the real margin figures and pre-fills the Job Calculator.</li>
  </ul>
  <p style="margin-top:10px">Click <strong>Save Changes</strong>. Costing appears immediately below the parts list: cost per part, assembly, true cost per item, suggested wholesale and retail, filament per item, print time per item, and the 40/50/60/70/80% price ladder.</p>
</section>

<section class="card">
  <div class="card-head"><h2>Reading the catalog</h2></div>
  <dl>
    <dt>Parts</dt>
    <dd>How many printed pieces make up the product. A <code>0</code> means it has no parts yet and cannot be costed or used on the Job Calculator.</dd>
    <dt>True Cost</dt>
    <dd>What one finished item actually costs you — all parts, plus assembly, plus scrap if you set a rate. Shown to three decimals because the numbers are small and rounding early hides real money.</dd>
    <dt>Selling</dt>
    <dd>Your Actual Selling Price, if you have set one.</dd>
    <dt>Margin</dt>
    <dd>Your real margin at that price. Green at 50% or better, gray below. If you have not set a price, this shows the suggested retail instead.</dd>
    <dt>Status</dt>
    <dd>Active or Archived. <strong>Only active products appear in the Job Calculator picker.</strong></dd>
  </dl>
  <p style="margin-top:10px">Search matches SKU, product name and material. Archive anything you have stopped selling rather than deleting it — archiving keeps the history intact and gets it out of the picker.</p>
</section>

<section class="card">
  <div class="card-head"><h2>Renaming a SKU</h2></div>
  <p>SKUs are editable in the Edit form. When you rename one, the system checks nothing else uses the new SKU, renames the product photo to match, and carries the bill of materials across with it.</p>
  <ul>
    <li>A new SKU must match <code>TYPE-NNN</code> — for example <code>SGN-004</code>. Anything else is rejected, which is what stops a typo like <code>SGl-001</code> founding a phantom type prefix.</li>
    <li>Older SKUs that predate this format can still be edited and saved as they are. The format is only enforced when the SKU actually changes.</li>
    <li><strong>Label print history keeps the old SKU.</strong> Those are historical records of what you printed on the day, and rewriting them would be a lie.</li>
  </ul>
</section>

<section class="card">
  <div class="card-head"><h2>Photos, CSV, and bulk editing</h2></div>
  <ul>
    <li><strong>Photos</strong> — add one per product via Edit (JPG, PNG or WEBP). It shows as a thumbnail in the catalog. Replacing a photo removes the old file automatically.</li>
    <li><strong>Export Catalog CSV</strong> — one row per <em>part</em>, so a two-part product exports as two rows sharing a SKU. Product-level values repeat on each row, and true cost and suggested retail are included as read-only columns. The Category column stores the product's collection.</li>
    <li><strong>Export for QuickBooks</strong> — filter by collection, select active products, and download one row per product. Choose the layout matching your QuickBooks Online sample: separate Category column, category in the product name, or just name and SKU. Complete required item types and accounts in QuickBooks. This export does not set quantities or opening balances.</li>
    <li>
      <strong>Import CSV</strong> — required columns are <code>SKU</code> and <code>Product Name</code>.
      Any row carrying a <code>Qty on Plate</code> is treated as a part. The first row for a SKU
      replaces that SKU's existing parts and each later row adds to it, so the file is the source of
      truth for that product. Export, edit in a spreadsheet, re-import is a clean round trip.
    </li>
  </ul>
  <div class="callout">
    <strong>Import overwrites parts</strong>
    Importing a SKU that carries part data replaces that product's whole bill of materials. Export
    first if you are unsure, so you have the current state on disk.
  </div>
</section>

<section class="card">
  <div class="card-head"><h2>Using the product on a job</h2></div>
  <ol>
    <li>Go to <a href="{{ url_for('job_calculator') }}">Job Calculator</a>.</li>
    <li>Pick the SKU from the dropdown. Every print value fills itself from the catalog, <strong>locked</strong>, and your selling price pre-fills.</li>
    <li>Enter the job label, customer, order quantity and any packaging cost.</li>
    <li>If this particular run genuinely differed from the catalog, tick <strong>Override for this job</strong>. The saved job is flagged as overridden and keeps a snapshot of what you actually used. The catalog itself is never changed from there — edit the product for that.</li>
    <li><strong>Calculate</strong> to check the numbers, or <strong>Calculate &amp; Save to History</strong> to keep it. Saved jobs record the SKU and customer, and produce a PDF.</li>
  </ol>
  <div class="callout">
    <strong>Locked is the point</strong>
    Typing print values from memory on every job is how a catalog and its quotes drift apart. Locked
    by default means a quote always matches the product record unless you deliberately said otherwise.
  </div>
</section>

<section class="card">
  <div class="card-head"><h2>Rules worth remembering</h2></div>
  <ul>
    <li>Plate figures, never per-item figures.</li>
    <li>Qty per Product is how many of that part are in one finished item — not how many print at once.</li>
    <li>A product with zero parts cannot be costed and will not appear on the Job Calculator.</li>
    <li>Only active products appear in the Job Calculator picker.</li>
    <li>Changing a Settings rate re-prices the whole catalog instantly; saved jobs keep their quoted numbers.</li>
    <li>Archive, don't delete.</li>
    <li>Name your print files after the SKU.</li>
  </ul>
</section>

</div>
{% endblock %}
"""
@app.get("/guide")
def guide():
    # Step-by-step guide for the Products page and the costing workflow, linked
    # from Products with target=_blank. The markup is held inline rather than in
    # templates/ deliberately: it means deploying app.py alone delivers the guide,
    # with no second file that can fail to land and leave a dead link behind.
    return render_template_string(GUIDE_TEMPLATE, **costing_readiness())
@app.route("/products", methods=["GET", "POST"])
def products():
    from product_collections import canonical_collection, catalog_rows, collection_options
    con = db()
    # Keeps whatever was typed so a rejected add (duplicate SKU, missing field) re-renders
    # the form with the work still in it instead of silently throwing it away.
    add_form = {k: "" for k in ("sku", "product_name", "material", "notes", "category",
                                 "department",
                                 "qty_on_plate", "filament_used_g", "filament_cost",
                                 "print_time_text", "active_labor_min")}
    if request.method == "POST":
        for k in add_form:
            add_form[k] = request.form.get(k, "").strip()
        sku = add_form["sku"].upper()
        add_form["sku"] = sku
        name = add_form["product_name"]
        material = add_form["material"]
        notes = add_form["notes"]
        category = canonical_collection(con, add_form["category"])
        add_form["category"] = category
        # Free text, not validated against a fixed list, and never required —
        # a shared component genuinely has none. See products.department.
        department = add_form["department"]
        def _f(field):
            v = add_form.get(field, "")
            if v == "":
                return None
            try:
                return float(v)
            except ValueError:
                return None
        if not sku or not name:
            flash("SKU and Product Name are required.", "error")
        elif not SKU_RE.match(sku):
            flash(f"{sku} isn't a valid SKU — expected TYPE-NNN, e.g. SGN-003.", "error")
        else:
            try:
                con.execute("""
                    INSERT INTO products (sku, product_name, material, notes, category,
                                           department, active)
                    VALUES (?, ?, ?, ?, ?, ?, 1)
                """, (sku, name, material, notes, category, department))
                # If the Bambu Studio numbers were entered on the add form, create the
                # first part right away instead of making them come back through Edit.
                if _f("qty_on_plate"):
                    con.execute("""
                        INSERT INTO product_parts
                            (sku, part_name, sort_order, qty_per_product, qty_on_plate,
                             filament_used_g, filament_cost, print_time_text,
                             active_labor_min, material)
                        VALUES (?, 'Main', 0, 1, ?, ?, ?, ?, ?, ?)
                    """, (sku, _f("qty_on_plate"), _f("filament_used_g"), _f("filament_cost"),
                          add_form["print_time_text"], _f("active_labor_min"), material))
                con.commit()
                flash(f"Added {sku}.", "success")
                add_form = {k: "" for k in add_form}
            except sqlite3.IntegrityError:
                flash(f"{sku} already exists — your entries are still here, change the SKU and re-submit.", "error")
    q = request.args.get("q", "").strip()
    collection = request.args.get("collection", "").strip()
    collections = collection_options(con)
    rows = catalog_rows(con, q, collection)
    con2 = con
    settings = get_settings()
    products_with_pricing = []
    for r in rows:
        d = dict(r)
        d["parts"] = get_product_parts(con2, d["sku"])
        d["assembly_components"] = get_assembly_components(con2, d["sku"])
        if d["assembly_components"]:
            # An assembly's cost is the sum of what its components ACTUALLY cost
            # right now, from each component's own bill of materials — looked up
            # fresh on every page load, never cached on the assembly row. Change
            # a component's filament usage and every assembly built from it
            # reflects that the next time this page renders, with nothing to
            # update on the assembly itself.
            comp_lines = []
            for ac in d["assembly_components"]:
                crow = con2.execute("SELECT * FROM products WHERE sku=?",
                                    (ac["component_sku"],)).fetchone()
                if not crow:
                    continue
                cparts = get_product_parts(con2, ac["component_sku"])
                cpricing = calculate_product_cost(
                    cparts, crow["assembly_labor_min"], None, settings
                ) if cparts else None
                comp_lines.append({
                    "sku": ac["component_sku"],
                    "product_name": crow["product_name"],
                    "qty": ac["qty"],
                    "true_cost_per_item": (cpricing["true_cost_per_item"]
                                           if cpricing and not cpricing.get("error") else 0.0),
                })
            d["pricing"] = calculate_assembly_cost(
                comp_lines, d.get("assembly_labor_min"),
                d.get("actual_selling_price"), settings
            )
        elif d["parts"]:
            d["pricing"] = calculate_product_cost(
                d["parts"], d.get("assembly_labor_min"),
                d.get("actual_selling_price"), settings
            )
        else:
            d["pricing"] = None
        products_with_pricing.append(d)
    # Lab-average $/g across every priced pool. Used only to caption the Products
    # page as an estimate — a SKU has no color, so it has no specific spool to
    # price against until a job picks one.
    from filament import lab_average_cost_per_gram, pools_for_picker, pooled_cost_for_type
    avg_cpg = lab_average_cost_per_gram(con2)
    filament_pools = pools_for_picker(con2)
    # Pooled (material+color, blended across brands) rate per pool id — what
    # a linked part is actually priced from here, distinct from each pool's
    # own per-brand rate (which pools_for_picker's cost_per_gram field still
    # carries, unchanged, for anything that isn't this Products-page estimate).
    filament_pooled_rates = {f["id"]: pooled_cost_for_type(con2, f["id"]) for f in filament_pools}
    # Seeds each part's color-stack widget with whatever it's already linked
    # to, keyed the same way the template's color-stack elements are, so a
    # page reload doesn't lose a saved multi-color link.
    part_draws_seed = {}
    for d in products_with_pricing:
        for part in d["parts"]:
            draws = _part_filament_draws(part)
            if draws:
                part_draws_seed[f"part-{part['id']}"] = draws
    con2.close()
    return render_template("products.html", products=products_with_pricing, q=q,
                           collections=collections, collection=collection,
                           add_form=add_form, avg_cpg=avg_cpg,
                           filament_pools=filament_pools,
                           filament_pooled_rates=filament_pooled_rates,
                           part_draws_seed=part_draws_seed)
@app.post("/products/<sku>/edit")
def edit_product(sku):
    from product_collections import canonical_collection
    new_sku  = request.form.get("sku", "").strip().upper()
    name     = request.form.get("product_name", "").strip()
    material = request.form.get("material", "").strip()
    notes    = request.form.get("notes", "").strip()
    category = request.form.get("category", "").strip()
    department = request.form.get("department", "").strip()
    if not new_sku or not name:
        flash("SKU and Product Name are required.", "error")
        return redirect(url_for("products"))
    # Legacy SKUs (3DP-*, ones with spaces) predate the TYPE-NNN standard. Editing
    # one as-is is allowed so the catalog stays usable; the format is only enforced when
    # the SKU actually changes, so every rename has to land in the new standard.
    if new_sku != sku and not SKU_RE.match(new_sku):
        flash(f"{new_sku} isn't a valid SKU — expected TYPE-NNN, e.g. SGN-003.", "error")
        return redirect(url_for("products"))
    def to_float_or_none(field):
        v = request.form.get(field, "").strip()
        if v == "":
            return None
        try:
            return float(v)
        except ValueError:
            return None
    qty_on_plate         = to_float_or_none("qty_on_plate")
    filament_used_g      = to_float_or_none("filament_used_g")
    filament_cost        = to_float_or_none("filament_cost")
    print_time_text      = request.form.get("print_time_text", "").strip()
    active_labor_min     = to_float_or_none("active_labor_min")
    actual_selling_price = to_float_or_none("actual_selling_price")
    assembly_labor_min   = to_float_or_none("assembly_labor_min")
    stl_filename         = request.form.get("stl_filename", "").strip()
    threemf_filename     = request.form.get("threemf_filename", "").strip()
    nozzle_size          = to_float_or_none("nozzle_size")
    layer_height         = to_float_or_none("layer_height")
    con = db()
    category = canonical_collection(con, category)
    row = con.execute("SELECT * FROM products WHERE sku=?", (sku,)).fetchone()
    if not row:
        con.close()
        flash(f"SKU not found: {sku}", "error")
        return redirect(url_for("products"))
    photo_filename = row["photo_filename"] or ""
    if request.form.get("remove_photo") == "1":
        if photo_filename:
            existing = PHOTO_DIR / photo_filename
            if existing.exists():
                existing.unlink()
        photo_filename = ""
    else:
        # Upload against the OLD sku first; if renaming we'll move the file below
        uploaded = save_product_photo(sku, request.files.get("photo"))
        if uploaded == "invalid":
            con.close()
            flash("Photo must be JPG, PNG, or WEBP.", "error")
            return redirect(url_for("products"))
        elif uploaded:
            photo_filename = uploaded
    if new_sku != sku:
        # ── SKU rename ────────────────────────────────────────────────────────
        already = con.execute("SELECT 1 FROM products WHERE sku=?", (new_sku,)).fetchone()
        if already:
            con.close()
            flash(f"SKU {new_sku} already exists — pick a different one.", "error")
            return redirect(url_for("products"))
        # Rename the photo file so it stays linked to the new SKU
        if photo_filename:
            old_path = PHOTO_DIR / photo_filename
            ext = photo_filename.rsplit(".", 1)[-1] if "." in photo_filename else ""
            new_photo_filename = f"{new_sku}.{ext}" if ext else photo_filename
            if old_path.exists():
                old_path.rename(PHOTO_DIR / new_photo_filename)
            photo_filename = new_photo_filename
        # Copy row to new SKU, then delete old
        con.execute("""
            INSERT INTO products
                (sku, product_name, material, notes, category, department, active,
                 created_at, updated_at, qty_on_plate, filament_used_g,
                 filament_cost, print_time_text, active_labor_min,
                 actual_selling_price, stl_filename, threemf_filename,
                 nozzle_size, layer_height, photo_filename)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (new_sku, name, material, notes, category, department, row["active"],
              row["created_at"],
              row["qty_on_plate"], row["filament_used_g"], row["filament_cost"],
              row["print_time_text"], row["active_labor_min"], actual_selling_price,
              row["stl_filename"], row["threemf_filename"],
              row["nozzle_size"], row["layer_height"], photo_filename))
        # Carry the bill of materials across BEFORE deleting the old row, not
        # after. Reordered here: product_parts.sku now has a real FOREIGN KEY
        # (ON DELETE RESTRICT, added when foreign key enforcement was turned
        # on) — deleting a products row that still has parts pointing at it
        # is rejected. Reparenting first means no part ever points at a row
        # that's about to disappear, whether that row has 0 parts (true
        # today) or many.
        con.execute("UPDATE product_parts SET sku=? WHERE sku=?", (new_sku, sku))
        con.execute("DELETE FROM products WHERE sku=?", (sku,))
        con.execute("UPDATE products SET assembly_labor_min=? WHERE sku=?",
                    (assembly_labor_min, new_sku))
        con.commit()
        con.close()
        flash(f"SKU renamed {sku} → {new_sku} and saved.", "success")
        return redirect(url_for("products"))
    # ── Normal update (no rename) ─────────────────────────────────────────────
    # The legacy per-product print columns (qty_on_plate, filament_*, print_time_text,
    # active_labor_min, nozzle/layer, stl/3mf) are deliberately NOT written here any more —
    # that data lives on product_parts now. They're left frozen at their pre-BOM values so
    # a rollback still has something to fall back on.
    con.execute("""
        UPDATE products SET product_name=?, material=?, notes=?, category=?, department=?,
            actual_selling_price=?, photo_filename=?, assembly_labor_min=?,
            updated_at=CURRENT_TIMESTAMP
        WHERE sku=?
    """, (name, material, notes, category, department, actual_selling_price, photo_filename,
          assembly_labor_min, sku))
    con.commit()
    con.close()
    flash(f"Updated {new_sku}.", "success")
    return redirect(url_for("products"))
def _part_form_values():
    """Pulls the printed-part fields off a submitted form. Returns (values, error)."""
    def f(field):
        v = request.form.get(field, "").strip()
        if v == "":
            return None
        try:
            return float(v)
        except ValueError:
            return None
    values = {
        "part_name": request.form.get("part_name", "").strip() or "Part",
        "qty_per_product": f("qty_per_product") or 1,
        "qty_on_plate": f("qty_on_plate"),
        "filament_used_g": f("filament_used_g"),
        "filament_cost": f("filament_cost"),
        "print_time_text": request.form.get("print_time_text", "").strip(),
        "active_labor_min": f("active_labor_min"),
        "material": request.form.get("material", "").strip(),
        "nozzle_size": f("nozzle_size"),
        "layer_height": f("layer_height"),
        "stl_filename": request.form.get("stl_filename", "").strip(),
        "threemf_filename": request.form.get("threemf_filename", "").strip(),
        "notes": request.form.get("notes", "").strip(),
    }
    if not values["qty_on_plate"] or values["qty_on_plate"] <= 0:
        return values, "Qty on Plate must be greater than 0."
    if parse_print_time(values["print_time_text"]) is None:
        return values, 'Print Time must look like "1h 50m", "2h", or "45m".'
    return values, None
def _apply_filament_link(con, values, form):
    """Reads the filament picker/multi-draw fields a part form may carry and
    folds them into `values` in place, so add and edit do exactly the same
    thing rather than drifting apart. Reuses _color_draws() verbatim — same
    {prefix}filp_{pid}_{n}/{prefix}filg_{pid}_{n} fields the Job Calculator's
    own color stacks submit — but picked_pool here resolves to the
    material+color POOLED rate, not one brand's own rate, because this
    number feeds a Products-page ESTIMATE meant to blend brands of the same
    color, not a specific job drawing from a specific spool.

    No selection submitted at all clears the link (draws == []) — the same
    "blank means unlinked" convention used everywhere else in this app.
    filament_cost itself is never touched here: it stays whatever was typed,
    to serve as the fallback (and, once overridable, the override)."""
    from filament import pools_for_picker, pooled_cost_for_type
    pool_name = {f["id"]: f["label"] for f in pools_for_picker(con)}
    def picked_pool(key):
        raw = (form.get(key) or "").strip()
        if not raw.isdigit():
            return None, None, None
        fid = int(raw)
        return fid, pooled_cost_for_type(con, fid), pool_name.get(fid)
    draws, tot_g, _ = _color_draws(form, "part", values.get("filament_used_g"),
                                    picked_pool)
    if draws:
        values["filament_type_id"] = draws[0]["filament_type_id"]
        if len(draws) > 1:
            values["filament_used_g"] = tot_g
            values["filament_draws_json"] = json.dumps(draws)
        else:
            values["filament_draws_json"] = None
    else:
        values["filament_type_id"] = None
        values["filament_draws_json"] = None
    # Recorded intent, not inferred. Only meaningful when a pool is actually
    # linked, but stored as submitted either way — clearing the link later
    # and re-linking shouldn't silently resurrect a stale override choice.
    values["filament_cost_override"] = 1 if form.get("filament_cost_override") == "1" else 0
@app.post("/products/<sku>/parts/add")
def add_product_part(sku):
    con = db()
    if not con.execute("SELECT 1 FROM products WHERE sku=?", (sku,)).fetchone():
        con.close()
        flash(f"SKU not found: {sku}", "error")
        return redirect(url_for("products"))
    values, err = _part_form_values()
    if err:
        con.close()
        flash(f"{values['part_name']}: {err}", "error")
        return redirect(url_for("products"))
    _apply_filament_link(con, values, request.form)
    next_order = con.execute(
        "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM product_parts WHERE sku=?", (sku,)
    ).fetchone()[0]
    con.execute("""
        INSERT INTO product_parts
            (sku, part_name, sort_order, qty_per_product, qty_on_plate, filament_used_g,
             filament_cost, filament_type_id, filament_draws_json, filament_cost_override,
             print_time_text, active_labor_min, material, nozzle_size, layer_height,
             stl_filename, threemf_filename, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (sku, values["part_name"], next_order, values["qty_per_product"],
          values["qty_on_plate"], values["filament_used_g"], values["filament_cost"],
          values["filament_type_id"], values["filament_draws_json"],
          values["filament_cost_override"],
          values["print_time_text"], values["active_labor_min"], values["material"],
          values["nozzle_size"], values["layer_height"], values["stl_filename"],
          values["threemf_filename"], values["notes"]))
    con.execute("UPDATE products SET updated_at=CURRENT_TIMESTAMP WHERE sku=?", (sku,))
    con.commit()
    con.close()
    flash(f"Added part \"{values['part_name']}\" to {sku}.", "success")
    return redirect(url_for("products"))
@app.post("/parts/<int:part_id>/edit")
def edit_product_part(part_id):
    con = db()
    row = con.execute("SELECT sku, part_name FROM product_parts WHERE id=?", (part_id,)).fetchone()
    if not row:
        con.close()
        flash("Part not found.", "error")
        return redirect(url_for("products"))
    values, err = _part_form_values()
    if err:
        con.close()
        flash(f"{values['part_name']}: {err}", "error")
        return redirect(url_for("products"))
    _apply_filament_link(con, values, request.form)
    con.execute("""
        UPDATE product_parts SET part_name=?, qty_per_product=?, qty_on_plate=?,
            filament_used_g=?, filament_cost=?, filament_type_id=?, filament_draws_json=?,
            filament_cost_override=?,
            print_time_text=?, active_labor_min=?,
            material=?, nozzle_size=?, layer_height=?, stl_filename=?, threemf_filename=?,
            notes=?
        WHERE id=?
    """, (values["part_name"], values["qty_per_product"], values["qty_on_plate"],
          values["filament_used_g"], values["filament_cost"], values["filament_type_id"],
          values["filament_draws_json"], values["filament_cost_override"],
          values["print_time_text"],
          values["active_labor_min"], values["material"], values["nozzle_size"],
          values["layer_height"], values["stl_filename"], values["threemf_filename"],
          values["notes"], part_id))
    con.execute("UPDATE products SET updated_at=CURRENT_TIMESTAMP WHERE sku=?", (row["sku"],))
    con.commit()
    con.close()
    flash(f"Updated part \"{values['part_name']}\".", "success")
    return redirect(url_for("products"))
@app.post("/parts/<int:part_id>/delete")
def delete_product_part(part_id):
    con = db()
    row = con.execute("SELECT sku, part_name FROM product_parts WHERE id=?", (part_id,)).fetchone()
    if not row:
        con.close()
        flash("Part not found.", "error")
        return redirect(url_for("products"))
    remaining = con.execute("SELECT COUNT(*) FROM product_parts WHERE sku=?", (row["sku"],)).fetchone()[0]
    if remaining <= 1:
        con.close()
        flash("A product needs at least one part — edit this one instead of deleting it.", "error")
        return redirect(url_for("products"))
    con.execute("DELETE FROM product_parts WHERE id=?", (part_id,))
    con.execute("UPDATE products SET updated_at=CURRENT_TIMESTAMP WHERE sku=?", (row["sku"],))
    con.commit()
    con.close()
    flash(f"Removed part \"{row['part_name']}\" from {row['sku']}.", "success")
    return redirect(url_for("products"))
def _add_assembly_component_row(con, sku, component_sku, qty):
    """One component into one assembly. One level of nesting only, enforced
    here, in both directions, before the row is ever written:
      - a component can't itself already be an assembly (have its own
        components), and
      - a product that is already someone else's component can't be given
        components of its own.
    Neither direction is something a FOREIGN KEY can express — they're both
    facts about OTHER rows in this same table, not about the row being
    inserted — so this has to be application logic, not a constraint.
    Does not commit. Returns (ok, message)."""
    if not component_sku:
        return False, "Pick a component SKU."
    if qty is None or not math.isfinite(qty) or qty <= 0:
        return False, "Qty must be a number greater than 0."
    if component_sku == sku:
        return False, "A product can't be a component of itself."
    if not con.execute("SELECT 1 FROM products WHERE sku=?", (component_sku,)).fetchone():
        return False, f"Component SKU not found: {component_sku}"
    if con.execute("SELECT 1 FROM assembly_components WHERE assembly_sku=?",
                   (component_sku,)).fetchone():
        return False, (f"{component_sku} already has its own components, which makes it "
                       f"an assembly — an assembly can't be used as a component of "
                       f"another assembly (one level of nesting only).")
    if con.execute("SELECT 1 FROM assembly_components WHERE component_sku=?",
                   (sku,)).fetchone():
        return False, (f"{sku} is itself a component of another assembly, so it can't "
                       f"also have components of its own (one level of nesting only).")
    next_order = con.execute(
        "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM assembly_components WHERE assembly_sku=?",
        (sku,)).fetchone()[0]
    try:
        con.execute("""
            INSERT INTO assembly_components (assembly_sku, component_sku, qty, sort_order)
            VALUES (?, ?, ?, ?)
        """, (sku, component_sku, qty, next_order))
    except sqlite3.IntegrityError as e:
        return False, f"Could not add component: {e}"
    return True, f"Added {component_sku} ×{qty:g} to {sku}."
@app.post("/products/<sku>/components/add")
def add_assembly_component(sku):
    con = db()
    if not con.execute("SELECT 1 FROM products WHERE sku=?", (sku,)).fetchone():
        con.close()
        flash(f"SKU not found: {sku}", "error")
        return redirect(url_for("products"))
    component_sku = request.form.get("component_sku", "").strip().upper()
    qty_raw = request.form.get("qty", "").strip()
    try:
        qty = float(qty_raw) if qty_raw else 1.0
    except ValueError:
        qty = None
    ok, msg = _add_assembly_component_row(con, sku, component_sku, qty)
    if ok:
        con.execute("UPDATE products SET updated_at=CURRENT_TIMESTAMP WHERE sku=?", (sku,))
        con.commit()
    flash(msg, "success" if ok else "error")
    con.close()
    return redirect(url_for("products"))
@app.post("/assembly-components/<int:comp_id>/delete")
def delete_assembly_component(comp_id):
    con = db()
    row = con.execute(
        "SELECT assembly_sku, component_sku FROM assembly_components WHERE id=?",
        (comp_id,)).fetchone()
    if row:
        con.execute("DELETE FROM assembly_components WHERE id=?", (comp_id,))
        con.execute("UPDATE products SET updated_at=CURRENT_TIMESTAMP WHERE sku=?",
                    (row["assembly_sku"],))
        con.commit()
        flash(f"Removed {row['component_sku']} from {row['assembly_sku']}.", "success")
    else:
        flash("Component not found.", "error")
    con.close()
    return redirect(url_for("products"))
@app.post("/assemblies/save-from-job")
def save_assembly_from_job():
    """'Save these parts as an assembly', from the Job Calculator. Users have
    already picked N catalog SKUs for one job; this names that set and
    stores it in assembly_components via the exact same
    _add_assembly_component_row() path the Products page uses, so it gets
    the same one-level-nesting enforcement for free. Creates the assembly's
    own product row if the SKU is new; reuses it as-is if it already exists
    (never overwrites an existing product's name/department). Redirects back
    to the Job Calculator so the new assembly is immediately pickable."""
    sku = request.form.get("sku", "").strip().upper()
    name = request.form.get("product_name", "").strip()
    department = request.form.get("department", "").strip()
    component_skus = [s.strip().upper() for s in request.form.getlist("component_sku") if s.strip()]
    component_skus = list(dict.fromkeys(component_skus))    # dedupe, keep order
    if not sku or not name:
        flash("Assembly SKU and name are both required.", "error")
        return redirect(url_for("job_calculator"))
    if not SKU_RE.match(sku):
        flash(f"{sku} isn't a valid SKU — expected TYPE-NNN, e.g. SGN-003.", "error")
        return redirect(url_for("job_calculator"))
    if not component_skus:
        flash("Pick at least one catalog product before saving as an assembly.", "error")
        return redirect(url_for("job_calculator"))
    con = db()
    existed = con.execute("SELECT 1 FROM products WHERE sku=?", (sku,)).fetchone()
    if not existed:
        con.execute("""
            INSERT INTO products (sku, product_name, department, active)
            VALUES (?, ?, ?, 1)
        """, (sku, name, department))
    added, failed = [], []
    for csku in component_skus:
        ok, msg = _add_assembly_component_row(con, sku, csku, 1.0)
        (added if ok else failed).append(csku if ok else msg)
    con.execute("UPDATE products SET updated_at=CURRENT_TIMESTAMP WHERE sku=?", (sku,))
    con.commit()
    con.close()
    verb = "Reused existing" if existed else "Created"
    msg = f"{verb} assembly {sku}. Added: {', '.join(added) or 'none'}."
    if failed:
        msg += f" Could not add: {'; '.join(failed)}"
        flash(msg, "error")
    else:
        flash(msg, "success")
    return redirect(url_for("job_calculator", sku=sku))
@app.get("/api/product/<sku>")
def api_product_detail(sku):
    """Full costing detail for one SKU, including its bill of materials. This is what
    the Job Calculator's SKU picker pulls so job numbers come from the catalog instead
    of being retyped from memory."""
    con = db()
    row = con.execute("SELECT * FROM products WHERE sku=?", (sku,)).fetchone()
    if not row:
        con.close()
        return jsonify({"error": f"SKU not found: {sku}"}), 404
    parts = get_product_parts(con, sku)
    con.close()
    d = dict(row)
    settings = get_settings()
    pricing = calculate_product_cost(parts, d.get("assembly_labor_min"),
                                      d.get("actual_selling_price"), settings) if parts else None
    return jsonify({
        "sku": d["sku"],
        "product_name": d["product_name"],
        "material": d.get("material") or "",
        "category": d.get("category") or "",
        "department": d.get("department") or "",
        "assembly_labor_min": d.get("assembly_labor_min"),
        "actual_selling_price": d.get("actual_selling_price"),
        "parts": parts,
        "pricing": pricing,
    })
@app.get("/api/job-parts/<sku>")
def api_job_parts(sku):
    """What the Job Calculator's top multi-select and per-row SKU picker
    actually fetch. Read-only — resolves one catalog SKU (plain product or
    assembly) into job rows via _job_rows_for_sku(), never touching
    product_parts or assembly_components. An assembly returns its
    components' own parts, already qty-multiplied, so picking one SKU is
    exactly equivalent to picking every one of its components by hand."""
    con = db()
    rows, product_name, is_assembly, error = _job_rows_for_sku(con, sku)
    con.close()
    if error:
        return jsonify({"error": error}), 404
    return jsonify({"sku": sku.strip().upper(), "product_name": product_name,
                    "is_assembly": is_assembly, "parts": rows})
@app.get("/photos/<path:filename>")
def product_photo(filename):
    photo_path = PHOTO_DIR / filename
    if not photo_path.resolve().is_relative_to(PHOTO_DIR.resolve()) or not photo_path.exists():
        flash("Photo not found.", "error")
        return redirect(url_for("products"))
    return send_file(photo_path)
@app.post("/products/<sku>/toggle")
def toggle_product(sku):
    con = db()
    row = con.execute("SELECT active FROM products WHERE sku=?", (sku,)).fetchone()
    if row:
        con.execute("UPDATE products SET active=?, updated_at=CURRENT_TIMESTAMP WHERE sku=?",
                    (0 if row["active"] else 1, sku))
        con.commit()
    con.close()
    return redirect(url_for("products"))
from catalog_csv import register_catalog_csv
register_catalog_csv(app)
@app.route("/history")
def history():
    con = db()
    rows = con.execute("SELECT * FROM print_jobs ORDER BY id DESC LIMIT 250").fetchall()
    con.close()
    jobs = []
    for r in rows:
        d = dict(r)
        d["line_items"] = json.loads(d["payload_json"])
        jobs.append(d)
    return render_template("history.html", jobs=jobs)
@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        values = {
            "x_offset_mm": request.form.get("x_offset_mm", "0"),
            "y_offset_mm": request.form.get("y_offset_mm", "0"),
            "show_sku": "1" if request.form.get("show_sku") else "0",
            "show_qty": "1" if request.form.get("show_qty") else "0",
            "show_qr": "1" if request.form.get("show_qr") else "0",
            "qr_prefix": request.form.get("qr_prefix", "").strip(),
            "company_name": request.form.get("company_name", "").strip(),
            "labor_rate": request.form.get("labor_rate", "25"),
            "electricity_rate": request.form.get("electricity_rate", "0.15"),
            "printer_watts": request.form.get("printer_watts", "120"),
            "machine_wear_rate": request.form.get("machine_wear_rate", "0.1"),
            "default_wholesale_margin": request.form.get("default_wholesale_margin", "0.6"),
            "default_retail_margin": request.form.get("default_retail_margin", "0.7"),
            "scrap_rate": request.form.get("scrap_rate", "0"),
            "sales_tax_rate": request.form.get("sales_tax_rate", "0"),
            "default_active_labor_min": request.form.get("default_active_labor_min", "2"),
            "design_rate": request.form.get("design_rate", "50"),
            "min_job_charge": request.form.get("min_job_charge", "40"),
        }
        con = db()
        for k, v in values.items():
            con.execute("""
                INSERT INTO settings (key,value) VALUES (?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """, (k, v))
        con.commit()
        con.close()
        flash("Settings saved.", "success")
        return redirect(url_for("settings"))
    return render_template("settings.html", settings=get_settings())
@app.get("/settings/calibration")
def calibration():
    labels_out = []
    for i in range(30):
        labels_out.append({
            "sku": f"POSITION-{i+1:02d}",
            "product_name": f"Position {i+1}",
            "qty": i+1
        })
    pdf = make_pdf(labels_out, 1, get_settings())
    return send_file(pdf, mimetype="application/pdf", as_attachment=True,
                     download_name="calibration-5160.pdf")
@app.route("/calculator", methods=["GET", "POST"])
def calculator():
    result = None
    form_values = {
        "qty_on_plate": "", "filament_used_g": "", "filament_cost": "",
        "print_time_text": "", "active_labor_min": "", "actual_selling_price": "",
    }
    if request.method == "POST":
        def to_float_or_none(field):
            v = request.form.get(field, "").strip()
            if v == "":
                return None
            try:
                return float(v)
            except ValueError:
                return None
        for k in form_values:
            form_values[k] = request.form.get(k, "").strip()
        qty_on_plate = to_float_or_none("qty_on_plate")
        filament_used_g = to_float_or_none("filament_used_g")
        filament_cost = to_float_or_none("filament_cost")
        print_time_text = request.form.get("print_time_text", "").strip()
        active_labor_min = to_float_or_none("active_labor_min")
        actual_selling_price = to_float_or_none("actual_selling_price")
        result = calculate_quick(
            qty_on_plate, filament_used_g, filament_cost, print_time_text,
            active_labor_min, actual_selling_price, get_settings()
        )
    return render_template("calculator.html", result=result, form_values=form_values)
@app.route("/job-calculator", methods=["GET", "POST"])
def job_calculator():
    """Every row on this page — whether it started as a catalog pick (top
    multi-select or a per-row SKU load) or was typed by hand — is a cp_* row
    and goes through the exact same _custom_parts_from_form() /
    calculate_job_cost_from_parts() path. There is no separate 'catalog SKU'
    branch any more: picking a product never locks the form, and catalog rows
    and freehand rows share one combined total because they are, by the time
    this function sees them, the same kind of row."""
    result = None
    settings = get_settings()
    form_values = {
        "job_label": "", "customer": "", "sku": "",
        # What the picked parts (Section 1) actually build, typed by the user —
        # purely descriptive, never read by calculate_job_cost_from_parts.
        "product_name": "", "department": "",
        "order_quantity": "", "selling_price_per_item": "",
        "sales_tax_rate": settings.get("sales_tax_rate", "0"), "other_job_cost": "0",
        "assembly_labor_min": "",
        # Design time is entered in minutes and charged once. Leave blank and the
        # page offers whatever unbilled time is already logged for this SKU or
        # customer — the log is the source, not your memory.
        "design_minutes": "", "is_custom_job": "",
    }
    def to_float_or_none(field, src):
        v = (src.get(field) or "").strip()
        if v == "":
            return None
        try:
            return float(v)
        except ValueError:
            return None
    # ── Editing a saved job ──────────────────────────────────────────────────
    # /job-history/<id>/edit redirects here with ?edit_job_id=<id>. Rehydrate
    # everything from the stored payload_json (see the save block below for
    # what it contains) rather than trusting a query string for the actual
    # numbers. GET only — a POST always means "calculate or save what's on
    # the form right now," edit_job_id along for the ride just tells the save
    # branch to UPDATE instead of INSERT.
    edit_job_id = request.args.get("edit_job_id", "").strip()
    edit_detail = None
    if edit_job_id and request.method == "GET":
        con_e = db()
        erow = con_e.execute("SELECT payload_json FROM job_costs WHERE id=?",
                             (edit_job_id,)).fetchone()
        con_e.close()
        if erow:
            edit_detail = json.loads(erow["payload_json"])
        else:
            flash(f"Job cost record {edit_job_id} not found.", "error")
            edit_job_id = ""
    # This "selected_sku" is only ever a PRIMARY sku now — used for
    # job_costs.sku, the design-time offer and the selling-price prefill. It
    # no longer decides which parts get costed; a job with three catalog
    # picks and a freehand row has no single SKU, and payload["source_skus"]
    # below is where the full set actually lives.
    if edit_detail is not None:
        selected_sku = (edit_detail.get("sku") or "").strip().upper()
    else:
        selected_sku = (request.values.get("sku") or "").strip().upper()
    product = None
    con = db()
    catalog = [dict(r) for r in con.execute("""
        SELECT p.sku, p.product_name, p.department, p.category,
               EXISTS(SELECT 1 FROM assembly_components ac
                      WHERE ac.assembly_sku = p.sku) AS is_assembly
        FROM products p WHERE p.active = 1 ORDER BY p.sku
    """).fetchall()]
    # Grouped/sorted for the part pickers only — display order, not identity.
    # No new field: the group is just the TYPE segment SKU_RE already
    # requires every SKU to have. Alphabetical by group, "Other" pinned last
    # so a malformed SKU is never hidden, just uncategorised; within a group,
    # sorted by the SKU's own numeric suffix so BASE-002 comes before
    # BASE-010 rather than a plain string sort getting that wrong.
    for c in catalog:
        c["group"] = sku_type_segment(c["sku"])
    def _catalog_sort_key(c):
        is_other = c["group"] == "Other"
        try:
            num = int(c["sku"].rsplit("-", 1)[1])
        except (ValueError, IndexError):
            num = 0
        return (1 if is_other else 0, "" if is_other else c["group"], num, c["sku"])
    catalog.sort(key=_catalog_sort_key)
    customers = [r[0] for r in con.execute(
        "SELECT DISTINCT customer FROM job_costs WHERE COALESCE(customer,'') <> '' ORDER BY customer").fetchall()]
    if selected_sku:
        prow = con.execute("SELECT * FROM products WHERE sku=?", (selected_sku,)).fetchone()
        if prow:
            product = dict(prow)
            form_values["sku"] = selected_sku
        else:
            flash(f"SKU not found: {selected_sku}", "error")
            selected_sku = ""
    con.close()
    if edit_detail is not None:
        # payload_json is a superset of form_values (it starts as dict(form_values)
        # at save time, see below) — every key here was genuinely typed by the user on
        # the original job, not reconstructed or guessed.
        for k in form_values:
            if k in edit_detail:
                form_values[k] = edit_detail[k]
        form_values["sku"] = selected_sku
    # Pools for the picker, plus their real $/g. Reloaded on every request so a
    # spool bought five minutes ago prices this job correctly.
    from filament import pools_for_picker
    con3 = db()
    filaments = pools_for_picker(con3)
    con3.close()
    pool_cost = {f["id"]: f["cost_per_gram"] for f in filaments
                 if f["cost_per_gram"] is not None}
    pool_name = {f["id"]: f["label"] for f in filaments}

    def picked_pool(key):
        """Filament chosen for one part on this job. Returns (id, $/g, label)."""
        raw = (request.form.get(key) or "").strip()
        if not raw.isdigit():
            return None, None, None
        fid = int(raw)
        if fid not in pool_name:
            from flask import abort
            abort(400, description="The selected filament pool no longer exists.")
        return fid, pool_cost.get(fid), pool_name.get(fid)

    if request.method == "POST":
        for k in form_values:
            if k != "sku":
                form_values[k] = request.form.get(k, "").strip()
        form_values["sku"] = selected_sku
        job_label = form_values["job_label"] or "Untitled Job"
        customer = form_values["customer"]
        order_quantity = to_float_or_none("order_quantity", request.form)
        selling_price_per_item = to_float_or_none("selling_price_per_item", request.form) or 0.0
        sales_tax_percent = to_float_or_none("sales_tax_rate", request.form) or 0.0
        sales_tax_rate = sales_tax_percent / 100
        other_job_cost = to_float_or_none("other_job_cost", request.form) or 0.0
        order_quantity_int = int(order_quantity) if order_quantity else None
        design_minutes = to_float_or_none("design_minutes", request.form) or 0.0
        is_custom_job = form_values["is_custom_job"] == "1"
        job_parts = _custom_parts_from_form(request.form, picked_pool)
        if job_parts:
            result = calculate_job_cost_from_parts(
                order_quantity_int, selling_price_per_item, sales_tax_rate,
                other_job_cost, job_parts,
                to_float_or_none("assembly_labor_min", request.form),
                settings, design_minutes=design_minutes,
                is_custom_job=is_custom_job)
            result_parts_snapshot = job_parts
        else:
            result = {"error": "Add at least one part — name, qty on plate and print time."}
            result_parts_snapshot = []
        if request.form.get("action") == "save" and not result.get("error"):
            payload = dict(form_values)
            payload["job_label"] = job_label
            payload["customer"] = customer
            payload["sku"] = selected_sku
            # product_name/department are already in payload via dict(form_values)
            # above — what was typed in Section 2 for what these parts build,
            # not any one picked SKU's own catalog name.
            # The full set of catalog SKUs any row on this job actually came
            # from, deduped. payload["sku"] only ever holds one "primary" —
            # this is where a job built from several picks is actually
            # recorded, per row, via each row's own source_sku.
            payload["source_skus"] = sorted({p["source_sku"] for p in job_parts
                                             if p.get("source_sku")})
            # Snapshot the exact part values this quote was built from, so a later change
            # to the catalog never silently rewrites what you already quoted.
            payload["parts_snapshot"] = result_parts_snapshot
            payload["result"] = result
            save_job_id = request.form.get("edit_job_id", "").strip()
            con = db()
            try:
                if save_job_id:
                    if not con.execute("SELECT 1 FROM job_costs WHERE id=?",
                                       (save_job_id,)).fetchone():
                        raise ValueError(f"Job cost record {save_job_id} no longer exists.")
                    job_id = int(save_job_id)
                    # ── Undo this job's prior side effects first ──────────────
                    # Same reversal delete_job_cost already does. Runs inside
                    # THIS transaction, not committed on its own — a failure
                    # anywhere below (the UPDATE, the design-time rebilling,
                    # the filament re-deduction) must roll this back too, or
                    # the ledger ends up missing a job's stock movements with
                    # nothing on screen to say so.
                    con.execute("DELETE FROM filament_transactions WHERE job_cost_id=?",
                                (job_id,))
                    con.execute(
                        "UPDATE design_sessions SET billed_job_id=NULL WHERE billed_job_id=?",
                        (job_id,))
                    con.execute("""
                        UPDATE job_costs SET created_at=?, job_label=?, order_quantity=?,
                                              payload_json=?, sku=?, customer=?, overridden=?,
                                              design_minutes=?, design_fee=?, is_custom_job=?
                        WHERE id=?
                    """, (datetime.now().isoformat(timespec="seconds"), job_label,
                          order_quantity_int, json.dumps(payload), selected_sku, customer,
                          0, design_minutes, result.get("design_fee", 0.0),
                          1 if is_custom_job else 0, job_id))
                else:
                    cur = con.execute("""
                        INSERT INTO job_costs (created_at, job_label, order_quantity, payload_json,
                                                sku, customer, overridden,
                                                design_minutes, design_fee, is_custom_job)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (datetime.now().isoformat(timespec="seconds"), job_label, order_quantity_int,
                          json.dumps(payload), selected_sku, customer, 0,
                          design_minutes, result.get("design_fee", 0.0),
                          1 if is_custom_job else 0))
                    job_id = cur.lastrowid
                # ── Close out the design time this quote just charged for ─────────
                # Stamping billed_job_id is what stops the same hours being billed a
                # second time on the next quote. Only ever claims up to the minutes
                # actually charged, and only whole sessions, so a partial bill leaves
                # the remainder available rather than silently swallowing it.
                if design_minutes > 0:
                    _avail, _rows = unbilled_design_minutes(con, selected_sku, customer)
                    claimed = 0.0
                    for sess in _rows:
                        m = float(sess["minutes"] or 0)
                        if claimed + m > design_minutes + 0.01:
                            break
                        con.execute("UPDATE design_sessions SET billed_job_id=? WHERE id=?",
                                    (job_id, sess["id"]))
                        claimed += m
                # ── Filament deduction ────────────────────────────────────────────
                # Deducts what the job ACTUALLY burns: print runs x grams per plate,
                # per part. A 40-unit order of a 12-up part is 4 runs — 48 units of
                # filament, spares included. Scrap rate is already baked into the run
                # count upstream. Only parts linked to a filament move stock; the rest
                # are silently skipped, so a half-assigned catalog still saves fine.
                # No separate commit: the job and its stock movements land together or
                # not at all — and on an edit, that "all" now includes the reversal
                # above too, in the same transaction.
                from filament import record_pooled_movement
                deducted = []
                for line in (result.get("parts_plan") or []):
                    draws = line.get("filament_draws") or []
                    if not draws:
                        fid = line.get("filament_type_id")
                        g = line.get("filament_total_g") or 0
                        draws = [{"filament_type_id": fid,
                                  "filament_label": line.get("filament_label"),
                                  "grams": g}] if fid and g > 0 else []
                    for d in draws:
                        fid, grams = d.get("filament_type_id"), d.get("grams") or 0
                        if not fid or grams <= 0:
                            continue
                        label = d.get("filament_label") or "filament"
                        record_pooled_movement(con, fid, "job", -abs(grams), job_cost_id=job_id,
                                               note=f"{job_label} — {line['part_name']}"
                                                    + (f" ({label})" if len(draws) > 1 else ""))
                        deducted.append((label, grams))
                con.commit()
            except Exception as e:
                con.rollback()
                con.close()
                flash(f"Could not save \"{job_label}\" — nothing was changed. "
                      "Please check the server log.", "error")
                app.logger.exception("Could not save job cost")
                return redirect(url_for("job_calculator",
                                        edit_job_id=save_job_id) if save_job_id
                                else url_for("job_calculator"))
            con.close()
            verb = "Updated" if save_job_id else "Saved"
            msg = f"{verb} \"{job_label}\" in Job Cost History."
            if deducted:
                msg += " Deducted " + ", ".join(
                    f"{g:,.0f} g {lbl}" for lbl, g in deducted) + "."
            flash(msg, "success")
            return redirect(url_for("job_history"))
    elif product and edit_detail is None:
        # Landing on the page with a SKU picked — prefill the price we already know.
        # Not when editing: form_values already carries what this saved job was
        # actually priced at, and that must not be silently replaced by whatever
        # the catalog charges today.
        if product.get("actual_selling_price"):
            form_values["selling_price_per_item"] = product["actual_selling_price"]
    # Unbilled design time on offer for whatever is selected. Offered, never
    # applied automatically — charging a customer is your decision, not the app's.
    con4 = db()
    design_available, design_rows = unbilled_design_minutes(
        con4, selected_sku, form_values.get("customer", ""))
    con4.close()
    design_rate = float(settings.get("design_rate", 50))
    # custom_rows seeds every row the page renders. Priority: a saved job's
    # frozen parts_snapshot when editing (never re-derived from a catalog
    # that may have since changed) > the just-submitted form on a POST
    # reload > the picked SKU's catalog parts on a fresh GET (arriving from
    # the Products page, a job-history edit link, or Save-as-Assembly's own
    # redirect) > empty for a blank page.
    if edit_detail is not None:
        custom_rows = edit_detail.get("parts_snapshot") or []
    elif request.method == "POST":
        custom_rows = _custom_parts_from_form(request.form, picked_pool)
    elif selected_sku and product:
        con5 = db()
        custom_rows, _name, _is_asm, _err = _job_rows_for_sku(con5, selected_sku)
        con5.close()
    else:
        custom_rows = []
    # Qty Ordered / Runs in the Parts of Product table are pure DISPLAY of
    # calculate_job_cost_from_parts()'s own already-computed output — never a
    # second calculation. Same-order zip: parts_plan is built by iterating
    # these exact rows in calculate_job_cost_from_parts(), so index i here is
    # index i there. Sourced from the live result after a POST, or from a
    # saved job's own frozen result when editing (also its own past
    # calculation, not re-derived).
    plan_source = None
    if result and not result.get("error") and result.get("parts_plan"):
        plan_source = result["parts_plan"]
    elif edit_detail is not None:
        plan_source = (edit_detail.get("result") or {}).get("parts_plan")
    if plan_source:
        for row, plan in zip(custom_rows, plan_source):
            row["qty_ordered"] = plan.get("units_needed")
            row["runs"] = plan.get("print_runs_required")
    return render_template("job_calculator.html", result=result, form_values=form_values,
                           **costing_readiness(),
                           design_available_minutes=design_available,
                           design_available_sessions=len(design_rows),
                           design_rate=design_rate,
                           design_available_fee=(design_available / 60) * design_rate,
                           min_job_charge=float(settings.get("min_job_charge", 0)),
                           catalog=catalog, product=product,
                           customers=customers, settings=settings, filaments=filaments,
                           picked=request.form if request.method == "POST" else {},
                           edit_job_id=edit_job_id, custom_rows=custom_rows)
@app.route("/job-history")
def job_history():
    con = db()
    rows = con.execute("SELECT * FROM job_costs ORDER BY id DESC LIMIT 250").fetchall()
    con.close()
    jobs = []
    for r in rows:
        d = dict(r)
        d["detail"] = json.loads(d["payload_json"])
        jobs.append(d)
    return render_template("job_history.html", jobs=jobs)
@app.get("/job-history/<int:job_id>/edit")
def edit_job_cost_redirect(job_id):
    """A saved job cost is an internal pre-quote costing record, not a
    customer-facing quote or a financial document — there is no audit trail
    to preserve, so it is edited by loading it straight back into the
    calculator it was built in. All the real work happens in job_calculator()
    (see the edit_job_id handling there); this just points at it."""
    con = db()
    exists = con.execute("SELECT 1 FROM job_costs WHERE id=?", (job_id,)).fetchone()
    con.close()
    if not exists:
        flash(f"Job cost record {job_id} not found.", "error")
        return redirect(url_for("job_history"))
    return redirect(url_for("job_calculator", edit_job_id=job_id))
@app.post("/job-history/<int:job_id>/delete")
def delete_job_cost(job_id):
    con = db()
    row = con.execute("SELECT job_label FROM job_costs WHERE id=?", (job_id,)).fetchone()
    if row:
        # Deleting a job puts its filament back. Without this, deleting a saved
        # job would leave stock permanently short with no ledger row explaining
        # where it went.
        returned = con.execute("""SELECT COALESCE(SUM(grams),0) g FROM filament_transactions
                                  WHERE job_cost_id=?""", (job_id,)).fetchone()["g"]
        con.execute("DELETE FROM filament_transactions WHERE job_cost_id=?", (job_id,))
        # Design time goes back on the shelf with the filament. Deleting the quote
        # that billed it must not strand those hours as permanently unbillable.
        con.execute("UPDATE design_sessions SET billed_job_id=NULL WHERE billed_job_id=?",
                    (job_id,))
        con.execute("DELETE FROM job_costs WHERE id=?", (job_id,))
        con.commit()
        msg = f"Deleted \"{row['job_label']}\" from Job Cost History."
        if returned:
            msg += f" Returned {abs(returned):,.0f} g of filament to stock."
        flash(msg, "success")
    con.close()
    return redirect(url_for("job_history"))
@app.post("/job-history/clear")
def clear_job_history():
    con = db()
    count = con.execute("SELECT COUNT(*) FROM job_costs").fetchone()[0]
    con.execute("""DELETE FROM filament_transactions
                   WHERE job_cost_id IS NOT NULL""")
    con.execute("UPDATE design_sessions SET billed_job_id=NULL WHERE billed_job_id IS NOT NULL")
    con.execute("DELETE FROM job_costs")
    con.commit()
    con.close()
    flash(f"Cleared {count} job cost record(s) from history.", "success")
    return redirect(url_for("job_history"))
@app.get("/job-history/<int:job_id>/pdf")
def job_cost_pdf(job_id):
    con = db()
    row = con.execute("SELECT * FROM job_costs WHERE id=?", (job_id,)).fetchone()
    con.close()
    if not row:
        flash("Job cost record not found.", "error")
        return redirect(url_for("job_history"))
    detail = json.loads(row["payload_json"])
    pdf = make_job_cost_pdf(row["job_label"], row["created_at"], detail)
    safe_name = "".join(ch if ch.isalnum() else "-" for ch in row["job_label"])[:40]
    return send_file(pdf, mimetype="application/pdf", as_attachment=True,
                      download_name=f"job-cost-{safe_name}-{row['id']}.pdf")
@app.route("/design", methods=["GET"])
def design_log():
    con = db()
    running = open_design_session(con)
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM design_sessions ORDER BY id DESC LIMIT 200").fetchall()]
    catalog = [dict(r) for r in con.execute(
        "SELECT sku, product_name FROM products WHERE active=1 ORDER BY sku").fetchall()]
    customers = [r[0] for r in con.execute(
        """SELECT DISTINCT customer FROM job_costs WHERE COALESCE(customer,'') <> ''
           UNION SELECT DISTINCT customer FROM design_sessions
           WHERE COALESCE(customer,'') <> '' ORDER BY customer""").fetchall()]
    con.close()
    settings_now = get_settings()
    rate = float(settings_now.get("design_rate", 50))
    billable_unbilled = sum(float(r["minutes"] or 0) for r in rows
                            if r["billable"] and r["ended_at"] and not r["billed_job_id"])
    return render_template("design_log.html", running=running, sessions=rows,
                           catalog=catalog, customers=customers,
                           settings=settings_now, design_rate=rate,
                           unbilled_minutes=billable_unbilled,
                           unbilled_value=(billable_unbilled / 60) * rate)


@app.post("/design/start")
def design_start():
    con = db()
    if open_design_session(con):
        con.close()
        flash("A design clock is already running — stop it before starting another.", "error")
        return redirect(url_for("design_log"))
    now = datetime.now().isoformat(timespec="seconds")
    con.execute("""INSERT INTO design_sessions
                   (sku, customer, job_label, started_at, billable, note, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                ((request.form.get("sku") or "").strip().upper(),
                 (request.form.get("customer") or "").strip(),
                 (request.form.get("job_label") or "").strip(),
                 now,
                 0 if request.form.get("billable") == "0" else 1,
                 (request.form.get("note") or "").strip(),
                 now))
    con.commit()
    con.close()
    flash("Design clock started.", "success")
    return redirect(url_for("design_log"))


@app.post("/design/stop")
def design_stop():
    con = db()
    running = open_design_session(con)
    if not running:
        con.close()
        flash("No design clock is running.", "error")
        return redirect(url_for("design_log"))
    ended = datetime.now()
    started = datetime.fromisoformat(running["started_at"])
    minutes = round((ended - started).total_seconds() / 60.0, 2)
    note = (request.form.get("note") or "").strip() or running["note"]
    con.execute("""UPDATE design_sessions SET ended_at=?, minutes=?, note=?
                   WHERE id=?""",
                (ended.isoformat(timespec="seconds"), minutes, note, running["id"]))
    con.commit()
    con.close()
    flash(f"Design clock stopped — {minutes:,.0f} min logged.", "success")
    return redirect(url_for("design_log"))


@app.post("/design/add")
def design_add():
    """Manual entry for a session you forgot to time.

    Flagged estimated=1 without exception. A reconstructed number and a measured
    number are not the same evidence and must never look alike in the log."""
    try:
        minutes = float((request.form.get("minutes") or "").strip())
    except ValueError:
        flash("Minutes must be a number.", "error")
        return redirect(url_for("design_log"))
    if minutes <= 0:
        flash("Minutes must be greater than 0.", "error")
        return redirect(url_for("design_log"))
    now = datetime.now().isoformat(timespec="seconds")
    con = db()
    con.execute("""INSERT INTO design_sessions
                   (sku, customer, job_label, started_at, ended_at, minutes,
                    estimated, billable, note, created_at)
                   VALUES (?,?,?,?,?,?,1,?,?,?)""",
                ((request.form.get("sku") or "").strip().upper(),
                 (request.form.get("customer") or "").strip(),
                 (request.form.get("job_label") or "").strip(),
                 now, now, minutes,
                 0 if request.form.get("billable") == "0" else 1,
                 (request.form.get("note") or "").strip(),
                 now))
    con.commit()
    con.close()
    flash(f"Logged {minutes:,.0f} min as an estimate.", "success")
    return redirect(url_for("design_log"))


@app.post("/design/<int:session_id>/toggle-billable")
def design_toggle_billable(session_id):
    con = db()
    row = con.execute("SELECT * FROM design_sessions WHERE id=?", (session_id,)).fetchone()
    if not row:
        con.close()
        flash("Design session not found.", "error")
        return redirect(url_for("design_log"))
    if row["billed_job_id"]:
        con.close()
        flash("That session is already billed on a saved quote — delete the job first.", "error")
        return redirect(url_for("design_log"))
    con.execute("UPDATE design_sessions SET billable=? WHERE id=?",
                (0 if row["billable"] else 1, session_id))
    con.commit()
    con.close()
    flash("Billable flag updated.", "success")
    return redirect(url_for("design_log"))


@app.post("/design/<int:session_id>/delete")
def design_delete(session_id):
    con = db()
    row = con.execute("SELECT billed_job_id FROM design_sessions WHERE id=?",
                      (session_id,)).fetchone()
    if row and row["billed_job_id"]:
        con.close()
        flash("That session is billed on a saved quote — delete the job first.", "error")
        return redirect(url_for("design_log"))
    con.execute("DELETE FROM design_sessions WHERE id=?", (session_id,))
    con.commit()
    con.close()
    flash("Design session deleted.", "success")
    return redirect(url_for("design_log"))


@app.get("/api/design-minutes")
def api_design_minutes():
    """Unbilled billable design minutes for a SKU or customer, for the Job
    Calculator to pull in without you retyping it."""
    sku = (request.args.get("sku") or "").strip().upper()
    customer = (request.args.get("customer") or "").strip()
    con = db()
    minutes, rows = unbilled_design_minutes(con, sku, customer)
    con.close()
    rate = float(get_settings().get("design_rate", 50))
    return jsonify({"minutes": round(minutes, 2), "sessions": len(rows),
                    "design_rate": rate, "fee": round((minutes / 60) * rate, 2)})


@app.get("/backup")
def backup():
    from backup_tools import database_snapshot
    return send_file(database_snapshot(), as_attachment=True, download_name=f"3dprinttally-backup-{datetime.now().date()}.db", mimetype="application/octet-stream")
@app.get("/backup/full")
def backup_full():
    from backup_tools import full_backup
    return send_file(full_backup(), as_attachment=True, mimetype="application/zip",
                     download_name=f"3dprinttally-backup-{datetime.now().date()}.zip")

@app.context_processor
def inject_backup_status():
    from backup_tools import backup_status
    return {"backup_status": backup_status()}

@app.get("/api/products")
def api_products():
    q = request.args.get("q", "").strip()
    con = db()
    if q:
        rows = con.execute("""
            SELECT sku, product_name, material, department FROM products
            WHERE active=1 AND (sku LIKE ? OR product_name LIKE ?)
            ORDER BY product_name LIMIT 50
        """, (f"%{q}%", f"%{q}%")).fetchall()
    else:
        rows = con.execute("""
            SELECT sku, product_name, material, department FROM products
            WHERE active=1 ORDER BY product_name LIMIT 100
        """).fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])
@app.get("/api/next-sku")
def api_next_sku():
    """Return the next available SKU for a given Type prefix.
    Queries all existing SKUs matching TYPE-NNN and returns TYPE-(max+1).
    Department is no longer part of the SKU — see the SKU_RE comment near the
    top of this file — so it is not a parameter here either; it is set on the
    product separately, as its own field.
    Example: GET /api/next-sku?type_code=SGN -> {"sku": "SGN-003", "prefix": "SGN", "next_seq": 3}
    """
    type_code = request.args.get("type_code", "").strip().upper()
    if not type_code:
        return jsonify({"error": "type_code is required."}), 400
    prefix = type_code
    con = db()
    rows = con.execute(
        "SELECT sku FROM products WHERE sku LIKE ?", (f"{prefix}-%",)
    ).fetchall()
    con.close()
    max_seq = 0
    for r in rows:
        suffix = r["sku"][len(prefix) + 1:]   # everything after "PREFIX-"
        try:
            seq = int(suffix)
            if seq > max_seq:
                max_seq = seq
        except ValueError:
            pass   # suffix isn't a plain integer — skip it
    next_seq = max_seq + 1
    return jsonify({
        "sku":      f"{prefix}-{next_seq:03d}",
        "prefix":   prefix,
        "next_seq": next_seq,
    })
# ── Filament inventory ────────────────────────────────────────────────
# Lives in filament.py so this file stops growing. Registered here rather than
# at the top because the blueprint imports nothing from app.py — no cycle.
# Must stay ABOVE the __main__ block: gunicorn never runs that block, but
# `python app.py` would block on app.run() and never reach anything below it.
from filament import filament_bp
app.register_blueprint(filament_bp)
from quickbooks_export import register_quickbooks_export
register_quickbooks_export(app)

from source_distribution import register_source_distribution
register_source_distribution(app)

if __name__ == "__main__":
    init_db()
    app.run(host="127.0.0.1", port=8080)
