"""
Filament inventory for 3DPrintTally.
──────────────────────────────────────────────────────────────────────────────
Held in its own module deliberately. app.py is already ~1,800 lines; every
change there means regenerating the whole file and re-verifying code that was
never touched. The filament subsystem owns its own schema, its own routes and
its own helpers, so Phases 2 and 3 edit this file and nothing else.

MODEL
  filament_types         One row per brand purchase pool. "White PLA (Bambu)" remains a
                         matter how many spools you own. Identity is
                         material + color + brand, enforced by a unique index
                         so "White" and "white" can't found two pools.

  filament_transactions  Append-only ledger. Stock is SUM(grams) — there is no
                         stored balance column anywhere, on purpose. A stored
                         balance drifts the first time something half-fails and
                         then there is no way to audit how it got wrong.
                         kind is one of:
                           purchase   + grams, carries cost and spool count
                           job        - grams, links to job_costs.id (Phase 2)
                           waste      - grams, failed prints and purge
                           reconcile  ± grams, "the scale says X" corrections

COST
  Weighted average $/g = SUM(purchase cost) / SUM(purchase grams). It moves as
  you buy at different prices, which is correct, and it is only ever read — the
  ledger stores what you actually paid, never a derived rate.
"""
from flask import (Blueprint, render_template, request, redirect,
                   url_for, flash, Response)
import sqlite3, csv, io
from pathlib import Path
from datetime import datetime

from config import APP_DIR, DB_PATH

filament_bp = Blueprint("filament", __name__)

# Offered in the dropdown, but the field is a free-text datalist — type anything
# and it is accepted. This list exists to stop typos, not to limit you.
COMMON_MATERIALS = ["PLA", "PLA+", "PLA Silk", "PLA Matte", "PLA-CF",
                    "PETG", "PETG-CF", "TPU", "ABS", "ASA", "PC", "PA-CF"]

KINDS = ("purchase", "job", "waste", "reconcile", "personal")


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    # Same DB file as app.py's connections, but a separate Python connection
    # object — foreign_keys is per-connection, not persisted in the file, so
    # this needs the same pragma app.py's db() sets, independently.
    con.execute("PRAGMA foreign_keys = ON")
    return con


_schema_ready = False


def init_filament_db():
    """Idempotent. Runs once per gunicorn worker rather than once per request —
    app.py re-runs its own init on every request, which works but is wasteful."""
    global _schema_ready
    if _schema_ready:
        return
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = db()
    con.executescript("""
        CREATE TABLE IF NOT EXISTS filament_types (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            material TEXT NOT NULL,
            color_name TEXT NOT NULL,
            color_hex TEXT DEFAULT '',
            brand TEXT DEFAULT '',
            default_spool_g REAL NOT NULL DEFAULT 1000,
            reorder_threshold_g REAL NOT NULL DEFAULT 500,
            notes TEXT DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        -- Identity guard. Without this you get three "white PLA" pools inside a
        -- month and the reorder alerts become meaningless.
        CREATE UNIQUE INDEX IF NOT EXISTS idx_filament_identity
            ON filament_types(material, color_name, brand);

        CREATE TABLE IF NOT EXISTS filament_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filament_type_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            kind TEXT NOT NULL,
            grams REAL NOT NULL,
            cost REAL,
            spools REAL,
            job_cost_id INTEGER,
            note TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_filament_tx_type
            ON filament_transactions(filament_type_id);
        CREATE INDEX IF NOT EXISTS idx_filament_tx_job
            ON filament_transactions(job_cost_id);
    """)
    # ── product_parts link ────────────────────────────────────────────────
    # Nullable on purpose. A part with no filament assigned keeps its
    # hand-typed cost and behaves exactly as it did before Phase 2 — nothing
    # you have already costed moves because this column appeared.
    # app.py's own before_request runs first and creates product_parts, but the
    # existence check keeps this safe regardless of handler ordering.
    has_parts = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='product_parts'"
    ).fetchone()
    if has_parts:
        cols = {r[1] for r in con.execute("PRAGMA table_info(product_parts)").fetchall()}
        if "filament_type_id" not in cols:
            try:
                con.execute("ALTER TABLE product_parts ADD COLUMN filament_type_id INTEGER")
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e).lower():
                    raise
    con.commit()
    con.close()
    # Only latch once the parts link exists, so a first-request ordering fluke
    # retries instead of leaving the column permanently missing.
    _schema_ready = bool(has_parts)


@filament_bp.before_app_request
def _ensure_filament_schema():
    init_filament_db()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _f(field, default=None):
    """Float off the form, or default when blank/unparseable."""
    v = (request.form.get(field) or "").strip()
    if v == "":
        return default
    try:
        return float(v)
    except ValueError:
        return default


def stock_map(con):
    """{filament_type_id: grams on hand}. Absent key means no movement yet."""
    rows = con.execute("""
        SELECT filament_type_id AS tid, COALESCE(SUM(grams), 0) AS g
        FROM filament_transactions GROUP BY filament_type_id
    """).fetchall()
    return {r["tid"]: r["g"] for r in rows}


def _norm(value):
    return " ".join((value or "").strip().casefold().split())


def group_members(con, filament_type_id):
    """All brand rows sharing one material + color group."""
    row = con.execute("SELECT material, color_name FROM filament_types WHERE id=?",
                      (filament_type_id,)).fetchone()
    if not row:
        return []
    return con.execute("""SELECT * FROM filament_types
                         WHERE lower(trim(material))=? AND lower(trim(color_name))=?
                         ORDER BY id""", (_norm(row["material"]), _norm(row["color_name"]))).fetchall()


def pooled_stock(con, filament_type_id):
    stock = stock_map(con)
    return sum(stock.get(r["id"], 0.0) for r in group_members(con, filament_type_id))


def record_pooled_movement(con, filament_type_id, kind, grams, cost=None,
                           spools=None, job_cost_id=None, note=""):
    """Allocate pooled consumption over brand ledgers, preserving audit detail."""
    members = group_members(con, filament_type_id)
    if not members:
        raise ValueError("filament pool no longer exists")
    if grams >= 0:
        record_movement(con, members[0]["id"], kind, grams, cost=cost,
                        spools=spools, job_cost_id=job_cost_id, note=note)
        return
    stock = stock_map(con)
    remaining = abs(float(grams))
    available = sum(max(0.0, stock.get(r["id"], 0.0)) for r in members)
    if remaining > available + 1e-6:
        raise ValueError(f"Not enough pooled stock: requested {remaining:g} g, available {available:g} g")
    for member in sorted(members, key=lambda r: stock.get(r["id"], 0.0), reverse=True):
        take = min(remaining, max(0.0, stock.get(member["id"], 0.0)))
        if take <= 0:
            continue
        record_movement(con, member["id"], kind, -take, job_cost_id=job_cost_id, note=note)
        remaining -= take
        if remaining <= 1e-6:
            break


def cost_map(con):
    """{filament_type_id: weighted average $/g}. Only pools with a costed
    purchase appear — a pool you have never priced has no derived rate, and the
    caller must fall back rather than invent one. Cost per SPOOL is derived from
    this at display time (rate x spool size) rather than stored, because spool
    size can change between purchases and a stored per-spool price would be a
    lie the moment you buy a 5 kg roll."""
    rows = con.execute("""
        SELECT filament_type_id AS tid, SUM(cost) AS c, SUM(grams) AS g
        FROM filament_transactions
        WHERE kind = 'purchase' AND cost IS NOT NULL AND grams > 0
        GROUP BY filament_type_id
    """).fetchall()
    return {r["tid"]: (r["c"] / r["g"]) for r in rows if r["g"]}


def cost_per_gram(con, filament_type_id):
    """Single-pool lookup. Used by the Job Calculator, where the customer's
    colour choice lands on one specific brand's spool and the job should be
    priced from what THAT spool actually cost — not blended with any other
    brand of the same colour."""
    row = con.execute("""
        SELECT SUM(cost) AS c, SUM(grams) AS g
        FROM filament_transactions
        WHERE filament_type_id = ? AND kind = 'purchase'
              AND cost IS NOT NULL AND grams > 0
    """, (filament_type_id,)).fetchone()
    if row and row["g"]:
        return row["c"] / row["g"]
    return None


def cost_per_gram_pooled(con, material, color_name):
    """Weighted average $/g across every BRAND sharing this material+colour.

    A shop may buy the same colour from more than one brand at different prices —
    two vendors' PLA Green are two separate filament_types
    rows (separate stock, separate reorder tracking, on purpose) but ONE
    colour a customer would ask for. A product's cost ESTIMATE should reflect
    what that colour actually costs blended across every brand it's been
    bought in, not lock onto whichever brand happened to get linked on the
    part. Never stored — this is a live query over the same
    filament_transactions ledger cost_per_gram() reads, just grouped wider."""
    row = con.execute("""
        SELECT SUM(t.cost) AS c, SUM(t.grams) AS g
        FROM filament_transactions t
        JOIN filament_types f ON f.id = t.filament_type_id
        WHERE lower(trim(f.material)) = lower(trim(?))
              AND lower(trim(f.color_name)) = lower(trim(?))
              AND t.kind = 'purchase' AND t.cost IS NOT NULL AND t.grams > 0
    """, (material, color_name)).fetchone()
    if row and row["g"]:
        return row["c"] / row["g"]
    return None


def pooled_cost_for_type(con, filament_type_id):
    """cost_per_gram_pooled(), starting from a specific pool's id — what a
    product-part filament picker actually has on hand after a selection."""
    row = con.execute(
        "SELECT material, color_name FROM filament_types WHERE id=?",
        (filament_type_id,)).fetchone()
    if not row:
        return None
    return cost_per_gram_pooled(con, row["material"], row["color_name"])


def record_movement(con, filament_type_id, kind, grams, cost=None,
                    spools=None, job_cost_id=None, note=""):
    """Writes one ledger row. Does NOT commit — the caller owns the transaction
    so a job and its stock movement commit or fail together (Phase 2)."""
    if kind not in KINDS:
        raise ValueError(f"unknown movement kind: {kind}")
    con.execute("""
        INSERT INTO filament_transactions
            (filament_type_id, created_at, kind, grams, cost, spools,
             job_cost_id, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (filament_type_id, datetime.now().isoformat(timespec="seconds"),
          kind, grams, cost, spools, job_cost_id, note))


def pools_for_picker(con):
    """Active pools for a dropdown, each with its derived $/g so the Products
    page can show what assigning one will actually cost."""
    rows = con.execute("""SELECT * FROM filament_types WHERE active=1
                         ORDER BY lower(material), lower(color_name), id""").fetchall()
    out, seen = [], set()
    for r in rows:
        key = (_norm(r["material"]), _norm(r["color_name"]))
        if key in seen:
            continue
        seen.add(key)
        members = group_members(con, r["id"])
        d = dict(r)
        d["active"] = int(any(m["active"] for m in members))
        d["member_ids"] = [m["id"] for m in members]
        d["brands"] = [m["brand"] for m in members if m["brand"]]
        d["brand"] = ", ".join(d["brands"])
        d["label"] = f"{r['material']} {r['color_name']}" + (f" ({d['brand']})" if d["brand"] else "")
        d["cost_per_gram"] = pooled_cost_for_type(con, r["id"])
        d["grams_on_hand"] = pooled_stock(con, r["id"])
        d["group_key"] = "|".join(key)
        out.append(d)
    return out


def lab_average_cost_per_gram(con):
    """One blended $/g across every priced purchase in the lab.

    Used ONLY for the Products-page estimate. A SKU has no colour, so it has no
    particular spool to price against — this gives a ballpark. The Job Calculator
    prices against the actual pool the customer's colour choice lands on."""
    row = con.execute("""
        SELECT SUM(cost) AS c, SUM(grams) AS g FROM filament_transactions
        WHERE kind='purchase' AND cost IS NOT NULL AND grams > 0
    """).fetchone()
    if row and row["g"]:
        return row["c"] / row["g"]
    return None


def pool_label(row):
    bits = [row["material"], row["color_name"]]
    if row["brand"]:
        bits.append(f"({row['brand']})")
    return " ".join(b for b in bits if b)


def _pools(con, include_archived=True):
    """Every pool with stock, value and status folded in."""
    order = "active DESC, material, color_name"
    rows = con.execute(f"SELECT * FROM filament_types ORDER BY {order}, id").fetchall()
    stock, costs = stock_map(con), cost_map(con)
    out, seen = [], set()
    for r in rows:
        d = dict(r)
        key = (_norm(r["material"]), _norm(r["color_name"]))
        if key in seen:
            continue
        seen.add(key)
        members = group_members(con, r["id"])
        d["active"] = int(any(m["active"] for m in members))
        if not include_archived and not any(m["active"] for m in members):
            continue
        g = sum(stock.get(m["id"], 0.0) for m in members)
        cpg = pooled_cost_for_type(con, r["id"])
        spool_g = (sum((m["default_spool_g"] or 1000) * max(stock.get(m["id"], 0), 0)
                      for m in members) / g) if g else (d["default_spool_g"] or 1000)
        d["grams_on_hand"] = g
        d["spools_on_hand"] = g / spool_g if spool_g else 0
        d["cost_per_gram"] = cpg
        d["cost_per_spool"] = (cpg * spool_g) if cpg is not None else None
        d["value_on_hand"] = (g * cpg) if cpg is not None else None
        d["member_ids"] = [m["id"] for m in members]
        d["brands"] = [m["brand"] for m in members if m["brand"]]
        d["brand"] = ", ".join(d["brands"])
        d["label"] = f"{r['material']} {r['color_name']}" + (f" ({d['brand']})" if d["brand"] else "")
        d["group_key"] = "|".join(key)
        if g <= 0:
            d["status"], d["status_class"] = "Out", "red"
        elif g <= (d["reorder_threshold_g"] or 0):
            d["status"], d["status_class"] = "Reorder", "red"
        else:
            d["status"], d["status_class"] = "In stock", "green"
        out.append(d)
    return out


# ── Routes ────────────────────────────────────────────────────────────────────

@filament_bp.get("/filament")
def index():
    con = db()
    pools = _pools(con)
    con.close()
    active = [p for p in pools if p["active"]]
    totals = {
        "pool_count": len(active),
        "grams": sum(p["grams_on_hand"] for p in active),
        "value": sum(p["value_on_hand"] or 0 for p in active),
        "reorder": sum(1 for p in active if p["status"] in ("Reorder", "Out")),
    }
    return render_template("filament.html", pools=pools, totals=totals,
                           materials=COMMON_MATERIALS)


@filament_bp.post("/filament/add")
def add():
    material = (request.form.get("material") or "").strip()
    color_name = (request.form.get("color_name") or "").strip()
    brand = (request.form.get("brand") or "").strip()
    color_hex = (request.form.get("color_hex") or "").strip()
    notes = (request.form.get("notes") or "").strip()
    spool_g = _f("default_spool_g", 1000) or 1000
    threshold = _f("reorder_threshold_g", 500)
    threshold = 500 if threshold is None else threshold
    if not material or not color_name:
        flash("Material and Color are both required.", "error")
        return redirect(url_for("filament.index"))
    if spool_g <= 0:
        flash("Spool size must be greater than 0 g.", "error")
        return redirect(url_for("filament.index"))
    # Opening stock, both optional. Recording what you paid at the moment you
    # create the filament is the natural workflow — you have the receipt in your
    # hand. Leaving them blank creates an empty pool, same as before.
    open_spools = _f("open_spools")
    open_price = _f("open_price_per_spool")
    con = db()
    try:
        cur = con.execute("""
            INSERT INTO filament_types
                (material, color_name, color_hex, brand, default_spool_g,
                 reorder_threshold_g, notes, active)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1)
        """, (material, color_name, color_hex, brand, spool_g, threshold, notes))
        msg = f"Added {material} {color_name}{' (' + brand + ')' if brand else ''}"
        if open_spools and open_spools > 0:
            grams = open_spools * spool_g
            total = (open_price * open_spools) if open_price is not None else None
            record_movement(con, cur.lastrowid, "purchase", grams, cost=total,
                            spools=open_spools, note="Opening stock")
            msg += f" with {open_spools:g} spool(s) — {grams:,.0f} g"
            if total is not None:
                msg += f" at ${open_price:.2f}/spool"
        con.commit()
        flash(msg + ".", "success")
    except sqlite3.IntegrityError:
        flash(f"{material} {color_name}"
              f"{' (' + brand + ')' if brand else ''} already exists.", "error")
    finally:
        con.close()
    return redirect(url_for("filament.index"))


@filament_bp.post("/filament/<int:fid>/edit")
def edit(fid):
    material = (request.form.get("material") or "").strip()
    color_name = (request.form.get("color_name") or "").strip()
    brand = (request.form.get("brand") or "").strip()
    color_hex = (request.form.get("color_hex") or "").strip()
    notes = (request.form.get("notes") or "").strip()
    spool_g = _f("default_spool_g", 1000) or 1000
    threshold = _f("reorder_threshold_g", 500)
    threshold = 500 if threshold is None else threshold
    if not material or not color_name:
        flash("Material and Color are both required.", "error")
        return redirect(url_for("filament.index"))
    con = db()
    if not con.execute("SELECT 1 FROM filament_types WHERE id=?", (fid,)).fetchone():
        con.close()
        flash("Filament not found.", "error")
        return redirect(url_for("filament.index"))
    try:
        con.execute("""
            UPDATE filament_types SET material=?, color_name=?, color_hex=?,
                brand=?, default_spool_g=?, reorder_threshold_g=?, notes=?
            WHERE id=?
        """, (material, color_name, color_hex, brand, spool_g, threshold,
              notes, fid))
        con.commit()
        flash(f"Updated {material} {color_name}.", "success")
    except sqlite3.IntegrityError:
        flash("Another filament already uses that material, color and brand.",
              "error")
    finally:
        con.close()
    return redirect(url_for("filament.index"))


@filament_bp.post("/filament/<int:fid>/purchase")
def purchase(fid):
    """Add spools. Grams come from spools x grams-per-spool so the common case
    is typing '1' — but grams-per-spool is editable per purchase, because the
    day you buy a 5 kg roll a hardcoded 1000 would be a schema migration."""
    con = db()
    row = con.execute("SELECT * FROM filament_types WHERE id=?", (fid,)).fetchone()
    if not row:
        con.close()
        flash("Filament not found.", "error")
        return redirect(url_for("filament.index"))
    # The inventory row is pooled. If a brand is supplied, put this purchase
    # into that existing brand row; otherwise retain the representative brand.
    brand = (request.form.get("brand") or "").strip()
    if brand and brand != row["brand"]:
        existing = con.execute("""SELECT * FROM filament_types
                                 WHERE lower(trim(material))=? AND lower(trim(color_name))=?
                                   AND lower(trim(brand))=?""",
                               (_norm(row["material"]), _norm(row["color_name"]), _norm(brand))).fetchone()
        if existing:
            fid, row = existing["id"], existing
        else:
            cur = con.execute("""INSERT INTO filament_types
                (material,color_name,color_hex,brand,default_spool_g,reorder_threshold_g,notes,active)
                VALUES (?,?,?,?,?,?,?,1)""", (row["material"],row["color_name"],row["color_hex"],brand,
                                               row["default_spool_g"],row["reorder_threshold_g"],row["notes"]))
            fid = cur.lastrowid
            row = con.execute("SELECT * FROM filament_types WHERE id=?", (fid,)).fetchone()
    spools = _f("spools", 1) or 1
    grams_per_spool = _f("grams_per_spool", row["default_spool_g"]) or row["default_spool_g"]
    # Priced per spool because that is how filament is actually sold. The ledger
    # stores the line total so the weighted average stays correct when a single
    # entry covers several spools bought together.
    price_per_spool = _f("price_per_spool")
    total_cost = (price_per_spool * spools) if price_per_spool is not None else None
    note = (request.form.get("note") or "").strip()
    if spools <= 0 or grams_per_spool <= 0:
        con.close()
        flash("Spools and grams per spool must both be greater than 0.", "error")
        return redirect(url_for("filament.index"))
    grams = spools * grams_per_spool
    record_movement(con, fid, "purchase", grams, cost=total_cost,
                    spools=spools, note=note)
    con.commit()
    con.close()
    cost_txt = (f" at ${price_per_spool:.2f}/spool (${total_cost:.2f} total)"
                if price_per_spool is not None else "")
    flash(f"Added {spools:g} spool(s) — {grams:,.0f} g of "
          f"{pool_label(row)}{cost_txt}.", "success")
    return redirect(url_for("filament.index"))


@filament_bp.post("/filament/<int:fid>/adjust")
def adjust(fid):
    """Two corrections in one form.

    waste      — grams burned with nothing sellable to show for it: failed
                 prints, purge tower, the tail you can't print with.
    reconcile  — you put the spool on a scale. Enter what it ACTUALLY reads and
                 the ledger records the difference. This is the thing that keeps
                 the system honest, because sliced estimates never include purge
                 and the drift is one-directional."""
    mode = (request.form.get("mode") or "").strip()
    note = (request.form.get("note") or "").strip()
    con = db()
    row = con.execute("SELECT * FROM filament_types WHERE id=?", (fid,)).fetchone()
    if not row:
        con.close()
        flash("Filament not found.", "error")
        return redirect(url_for("filament.index"))
    if mode == "waste":
        grams = _f("waste_grams")
        if grams is None or grams <= 0:
            con.close()
            flash("Enter the grams wasted as a positive number.", "error")
            return redirect(url_for("filament.index"))
        record_pooled_movement(con, fid, "waste", -abs(grams),
                               note=note or "Waste / failed print")
        con.commit()
        con.close()
        flash(f"Logged {grams:,.0f} g wasted on {pool_label(row)}.", "success")
        return redirect(url_for("filament.index"))
    if mode == "personal":
        # Printing something for yourself. Comes off stock exactly like a job
        # does, but never touches costing — there is no customer and no price.
        grams = _f("personal_grams")
        if grams is None or grams <= 0:
            con.close()
            flash("Enter the grams used as a positive number.", "error")
            return redirect(url_for("filament.index"))
        record_pooled_movement(con, fid, "personal", -abs(grams),
                               note=note or "Personal print")
        con.commit()
        con.close()
        flash(f"Logged {grams:,.0f} g of {pool_label(row)} for personal use.",
              "success")
        return redirect(url_for("filament.index"))
    if mode == "reconcile":
        actual = _f("actual_grams")
        if actual is None or actual < 0:
            con.close()
            flash("Enter the measured grams on hand (0 or more).", "error")
            return redirect(url_for("filament.index"))
        current = pooled_stock(con, fid)
        delta = actual - current
        if abs(delta) < 0.005:
            con.close()
            flash(f"{pool_label(row)} already matches — no adjustment written.",
                  "success")
            return redirect(url_for("filament.index"))
        record_pooled_movement(con, fid, "reconcile", delta,
                               note=note or f"Scale reading {actual:,.0f} g")
        con.commit()
        con.close()
        flash(f"Reconciled {pool_label(row)}: {current:,.0f} g → "
              f"{actual:,.0f} g ({delta:+,.0f} g).", "success")
        return redirect(url_for("filament.index"))
    con.close()
    flash("Pick Waste, Personal Print or Reconcile.", "error")
    return redirect(url_for("filament.index"))


@filament_bp.post("/filament/<int:fid>/toggle")
def toggle(fid):
    con = db()
    row = con.execute("SELECT * FROM filament_types WHERE id=?", (fid,)).fetchone()
    if not row:
        con.close()
        flash("Filament not found.", "error")
        return redirect(url_for("filament.index"))
    new_state = 0 if row["active"] else 1
    con.execute("UPDATE filament_types SET active=? WHERE id=?", (new_state, fid))
    con.commit()
    con.close()
    flash(f"{pool_label(row)} {'restored' if new_state else 'archived'}.",
          "success")
    return redirect(url_for("filament.index"))


@filament_bp.post("/filament/<int:fid>/delete")
def delete(fid):
    """Blocked only when a real JOB has consumed from this pool — deleting then
    would rewrite what a customer order actually cost. Purchases, waste and
    reconciliations are all yours to undo: a typo on setup should never leave you
    stuck with a pool you can neither fix nor remove."""
    con = db()
    row = con.execute("SELECT * FROM filament_types WHERE id=?", (fid,)).fetchone()
    if not row:
        con.close()
        flash("Filament not found.", "error")
        return redirect(url_for("filament.index"))
    jobs = con.execute("""SELECT COUNT(*) FROM filament_transactions
                          WHERE filament_type_id=? AND kind='job'""", (fid,)).fetchone()[0]
    if jobs:
        con.close()
        flash(f"{pool_label(row)} has been consumed by {jobs} saved job(s) — "
              f"archive it instead so the job costs stay honest.", "error")
        return redirect(url_for("filament.index"))
    # Product parts can link a pool for their cost estimate (see app.py's
    # get_product_parts). The primary link has a real FOREIGN KEY now and
    # would raise its own IntegrityError below, but a SECOND colour on a
    # multi-draw part lives in filament_draws_json, which SQLite can't
    # constrain — checked here by hand so both cases get the same readable
    # message instead of the FK case surfacing as a raw constraint error.
    linked = con.execute(
        "SELECT COUNT(*) FROM product_parts WHERE filament_type_id=?", (fid,)
    ).fetchone()[0]
    if not linked:
        import json
        for r in con.execute(
            "SELECT filament_draws_json FROM product_parts WHERE filament_draws_json IS NOT NULL"
        ).fetchall():
            try:
                draws = json.loads(r["filament_draws_json"]) or []
            except (ValueError, TypeError):
                continue
            if any(d.get("filament_type_id") == fid for d in draws):
                linked += 1
    if linked:
        con.close()
        flash(f"{pool_label(row)} is linked to {linked} product part(s) — "
              f"unlink it on the Products page before deleting, or archive "
              f"this pool instead.", "error")
        return redirect(url_for("filament.index"))
    n = con.execute("SELECT COUNT(*) FROM filament_transactions WHERE filament_type_id=?",
                    (fid,)).fetchone()[0]
    con.execute("DELETE FROM filament_transactions WHERE filament_type_id=?", (fid,))
    con.execute("DELETE FROM filament_types WHERE id=?", (fid,))
    con.commit()
    con.close()
    extra = f" and its {n} ledger entr{'y' if n == 1 else 'ies'}" if n else ""
    flash(f"Deleted {pool_label(row)}{extra}.", "success")
    return redirect(url_for("filament.index"))


@filament_bp.post("/filament/tx/<int:tx_id>/edit")
def tx_edit(tx_id):
    """Correct a ledger entry after the fact. This is the escape hatch for the
    commonest mistake there is: logging spools and forgetting the price."""
    con = db()
    row = con.execute("SELECT * FROM filament_transactions WHERE id=?", (tx_id,)).fetchone()
    if not row:
        con.close()
        flash("Ledger entry not found.", "error")
        return redirect(url_for("filament.index"))
    fid = row["filament_type_id"]
    if row["job_cost_id"]:
        con.close()
        flash("This entry came from a saved job — edit or delete the job instead.",
              "error")
        return redirect(url_for("filament.history", fid=fid))
    note = (request.form.get("note") or "").strip()
    if row["kind"] == "purchase":
        spools = _f("spools", row["spools"] or 1) or 1
        grams_per_spool = _f("grams_per_spool")
        if grams_per_spool is None:
            grams_per_spool = (row["grams"] / row["spools"]) if row["spools"] else row["grams"]
        price_per_spool = _f("price_per_spool")
        if spools <= 0 or grams_per_spool <= 0:
            con.close()
            flash("Spools and grams per spool must both be greater than 0.", "error")
            return redirect(url_for("filament.history", fid=fid))
        grams = spools * grams_per_spool
        cost = (price_per_spool * spools) if price_per_spool is not None else None
        con.execute("""UPDATE filament_transactions
                       SET grams=?, cost=?, spools=?, note=? WHERE id=?""",
                    (grams, cost, spools, note, tx_id))
    else:
        # waste / reconcile — a signed gram figure and a note, nothing else.
        grams = _f("grams")
        if grams is None:
            con.close()
            flash("Enter the gram change for this entry.", "error")
            return redirect(url_for("filament.history", fid=fid))
        con.execute("UPDATE filament_transactions SET grams=?, note=? WHERE id=?",
                    (grams, note, tx_id))
    con.commit()
    con.close()
    flash("Ledger entry updated.", "success")
    return redirect(url_for("filament.history", fid=fid))


@filament_bp.post("/filament/tx/<int:tx_id>/delete")
def tx_delete(tx_id):
    con = db()
    row = con.execute("SELECT * FROM filament_transactions WHERE id=?", (tx_id,)).fetchone()
    if not row:
        con.close()
        flash("Ledger entry not found.", "error")
        return redirect(url_for("filament.index"))
    fid = row["filament_type_id"]
    if row["job_cost_id"]:
        con.close()
        flash("This entry came from a saved job — delete the job instead.", "error")
        return redirect(url_for("filament.history", fid=fid))
    con.execute("DELETE FROM filament_transactions WHERE id=?", (tx_id,))
    con.commit()
    con.close()
    flash(f"Removed a {row['kind']} entry of {row['grams']:+,.0f} g.", "success")
    return redirect(url_for("filament.history", fid=fid))


@filament_bp.get("/filament/<int:fid>/history")
def history(fid):
    con = db()
    row = con.execute("SELECT * FROM filament_types WHERE id=?", (fid,)).fetchone()
    if not row:
        con.close()
        flash("Filament not found.", "error")
        return redirect(url_for("filament.index"))
    member_ids = [r["id"] for r in group_members(con, fid)]
    placeholders = ",".join("?" for _ in member_ids)
    rows = con.execute(f"""SELECT * FROM filament_transactions
        WHERE filament_type_id IN ({placeholders}) ORDER BY id DESC""", member_ids).fetchall()
    con.close()
    # Running balance, computed newest-first so each row shows what was on hand
    # immediately after that movement.
    entries = [dict(r) for r in rows]
    running = sum(e["grams"] for e in entries)
    for e in entries:
        e["balance_after"] = running
        running -= e["grams"]
        e["cost_per_spool"] = (e["cost"] / e["spools"]) if (
            e["cost"] is not None and e["spools"]) else None
    pool = dict(row)
    pool["label"] = f"{row['material']} {row['color_name']} (all brands)"
    return render_template("filament_history.html", pool=pool, entries=entries)


@filament_bp.get("/filament/guide")
def guide():
    return render_template("filament_guide.html")


@filament_bp.get("/filament/export")
def export():
    con = db()
    pools = _pools(con)
    con.close()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Material", "Color", "Brand", "Grams On Hand", "Spools On Hand",
                "Spool Size (g)", "Reorder Threshold (g)", "Status",
                "Cost / Spool ($)", "Cost / g ($)", "Value On Hand ($)",
                "Active", "Notes"])
    for p in pools:
        w.writerow([
            p["material"], p["color_name"], p["brand"],
            f"{p['grams_on_hand']:.0f}", f"{p['spools_on_hand']:.2f}",
            f"{p['default_spool_g']:.0f}", f"{p['reorder_threshold_g']:.0f}",
            p["status"],
            f"{p['cost_per_spool']:.2f}" if p["cost_per_spool"] is not None else "",
            f"{p['cost_per_gram']:.5f}" if p["cost_per_gram"] is not None else "",
            f"{p['value_on_hand']:.2f}" if p["value_on_hand"] is not None else "",
            p["active"], p["notes"],
        ])
    stamp = datetime.now().strftime("%Y%m%d")
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             f"attachment; filename=filament-inventory-{stamp}.csv"})
