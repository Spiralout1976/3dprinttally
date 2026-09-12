import sqlite3
from config import DB_PATH, PHOTO_DIR, DEFAULT_SETTINGS
from database import db
from filelock import FileLock
def _init_db():
    from filament import init_filament_db
    init_filament_db()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    con = db()
    con.executescript("""
        CREATE TABLE IF NOT EXISTS products (
            sku TEXT PRIMARY KEY,
            product_name TEXT NOT NULL,
            material TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            category TEXT DEFAULT '',
            -- Which department sells this, e.g. "Meat", "Bakery". Empty/blank
            -- for a shared component that no single department owns — see the
            -- SKU_RE comment above. Never a placeholder code; either a real
            -- department or genuinely nothing.
            department TEXT DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS print_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            total_labels INTEGER NOT NULL,
            start_position INTEGER NOT NULL DEFAULT 1,
            payload_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS job_costs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            job_label TEXT NOT NULL,
            order_quantity INTEGER NOT NULL,
            payload_json TEXT NOT NULL
        );
        -- ── Bill of materials ────────────────────────────────────────────────
        -- One row per printed part that makes up a finished product. A simple
        -- one-piece product has exactly one row here; the meat sign holder has
        -- two (holder + stem). Product cost = sum of its parts.
        --
        -- FOREIGN KEY ... ON DELETE RESTRICT: there is no delete-product route
        -- in this app today (only toggle_product, which deactivates). RESTRICT
        -- means that if one is ever added, deleting a product with parts still
        -- attached fails loudly with a constraint error instead of silently
        -- cascading the delete into real, hand-entered costing data. A blocked
        -- delete is recoverable — you just deleted the parts first, on purpose.
        -- A cascaded delete of parts you didn't mean to lose is not.
        CREATE TABLE IF NOT EXISTS product_parts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku TEXT NOT NULL,
            part_name TEXT NOT NULL DEFAULT 'Main',
            sort_order INTEGER NOT NULL DEFAULT 0,
            qty_per_product REAL NOT NULL DEFAULT 1,
            qty_on_plate REAL,
            filament_used_g REAL,
            filament_cost REAL,
            filament_type_id INTEGER,
            filament_draws_json TEXT,
            filament_cost_override INTEGER NOT NULL DEFAULT 0,
            print_time_text TEXT DEFAULT '',
            active_labor_min REAL,
            material TEXT DEFAULT '',
            nozzle_size REAL,
            layer_height REAL,
            stl_filename TEXT DEFAULT '',
            threemf_filename TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            FOREIGN KEY (sku) REFERENCES products(sku) ON DELETE RESTRICT,
            -- Real FK, not just an app-level guard, matching the standard Item 1
            -- set for sku above. RESTRICT for the same reason: no route deletes a
            -- filament_types row without going through filament.delete()'s own
            -- guard first, so this only ever fires as a last-resort integrity
            -- backstop, not as the everyday UX.
            FOREIGN KEY (filament_type_id) REFERENCES filament_types(id) ON DELETE RESTRICT
        );
        CREATE INDEX IF NOT EXISTS idx_product_parts_sku ON product_parts(sku);
        -- ── Assemblies (kits) ────────────────────────────────────────────────
        -- A sellable product built from OTHER sellable products, one level
        -- only: a component may not itself be an assembly. Not enforceable
        -- declaratively in SQLite (no CHECK can see across rows) -- enforced
        -- in add_assembly_component() before every INSERT. This table simply
        -- didn't exist before this row was written, so unlike product_parts
        -- there is no retrofit needed here -- CREATE TABLE IF NOT EXISTS
        -- handles both a fresh database and this one.
        --
        -- Both FKs RESTRICT for the same reason as product_parts: no
        -- delete-product route exists yet, and deleting a product that is
        -- either side of an assembly relationship must fail loudly when one
        -- is eventually added, not silently unlink or cascade.
        CREATE TABLE IF NOT EXISTS assembly_components (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            assembly_sku TEXT NOT NULL,
            component_sku TEXT NOT NULL,
            qty REAL NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (assembly_sku) REFERENCES products(sku) ON DELETE RESTRICT,
            FOREIGN KEY (component_sku) REFERENCES products(sku) ON DELETE RESTRICT
        );
        CREATE INDEX IF NOT EXISTS idx_assembly_components_assembly ON assembly_components(assembly_sku);
        CREATE INDEX IF NOT EXISTS idx_assembly_components_component ON assembly_components(component_sku);
        -- ── Design time log ──────────────────────────────────────────────────
        -- One row per stretch of custom design work. Non-recurring by definition:
        -- these minutes bill once, as a separate line on the quote, and are never
        -- amortized into per-item cost.
        --
        -- billable=0 is the learning-time rule made enforceable. Time spent
        -- working out HOW to do something (tutorials, test geometry, reading) is
        -- tuition and is yours to pay. Time spent on the customer's actual part
        -- is billable. Both get logged — only billable rows reach a quote.
        --
        -- estimated=1 marks a session reconstructed after the fact rather than
        -- timed live, so a number you guessed at is never mistaken later for one
        -- the clock actually measured.
        --
        -- ended_at IS NULL means the clock is still running.
        CREATE TABLE IF NOT EXISTS design_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku TEXT DEFAULT '',
            customer TEXT DEFAULT '',
            job_label TEXT DEFAULT '',
            started_at TEXT,
            ended_at TEXT,
            minutes REAL,
            estimated INTEGER NOT NULL DEFAULT 0,
            billable INTEGER NOT NULL DEFAULT 1,
            billed_job_id INTEGER,
            note TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_design_sessions_sku ON design_sessions(sku);
        CREATE INDEX IF NOT EXISTS idx_design_sessions_open
            ON design_sessions(ended_at);
    """)
    # Migrate databases created by Label Maker v1 without losing product data.
    # add_column_if_missing tolerates the race where two gunicorn workers both run
    # init_db() at startup and both attempt to add the same column at nearly the same
    # instant — one wins, the other would otherwise crash on "duplicate column name".
    def add_column_if_missing(column_name, ddl):
        try:
            con.execute(ddl)
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                raise
    product_columns = {r[1] for r in con.execute("PRAGMA table_info(products)").fetchall()}
    if "notes" not in product_columns:
        add_column_if_missing("notes", "ALTER TABLE products ADD COLUMN notes TEXT DEFAULT ''")
    if "created_at" not in product_columns:
        add_column_if_missing("created_at", "ALTER TABLE products ADD COLUMN created_at TEXT")
        con.execute("UPDATE products SET created_at=CURRENT_TIMESTAMP WHERE created_at IS NULL")
    if "updated_at" not in product_columns:
        add_column_if_missing("updated_at", "ALTER TABLE products ADD COLUMN updated_at TEXT")
        con.execute("UPDATE products SET updated_at=CURRENT_TIMESTAMP WHERE updated_at IS NULL")
    if "qty_on_plate" not in product_columns:
        add_column_if_missing("qty_on_plate", "ALTER TABLE products ADD COLUMN qty_on_plate REAL")
    if "filament_used_g" not in product_columns:
        add_column_if_missing("filament_used_g", "ALTER TABLE products ADD COLUMN filament_used_g REAL")
    if "filament_cost" not in product_columns:
        add_column_if_missing("filament_cost", "ALTER TABLE products ADD COLUMN filament_cost REAL")
    if "print_time_text" not in product_columns:
        add_column_if_missing("print_time_text", "ALTER TABLE products ADD COLUMN print_time_text TEXT DEFAULT ''")
    if "active_labor_min" not in product_columns:
        add_column_if_missing("active_labor_min", "ALTER TABLE products ADD COLUMN active_labor_min REAL")
    if "actual_selling_price" not in product_columns:
        add_column_if_missing("actual_selling_price", "ALTER TABLE products ADD COLUMN actual_selling_price REAL")
    if "stl_filename" not in product_columns:
        add_column_if_missing("stl_filename", "ALTER TABLE products ADD COLUMN stl_filename TEXT DEFAULT ''")
    if "threemf_filename" not in product_columns:
        add_column_if_missing("threemf_filename", "ALTER TABLE products ADD COLUMN threemf_filename TEXT DEFAULT ''")
    if "nozzle_size" not in product_columns:
        add_column_if_missing("nozzle_size", "ALTER TABLE products ADD COLUMN nozzle_size REAL")
    if "layer_height" not in product_columns:
        add_column_if_missing("layer_height", "ALTER TABLE products ADD COLUMN layer_height REAL")
    if "photo_filename" not in product_columns:
        add_column_if_missing("photo_filename", "ALTER TABLE products ADD COLUMN photo_filename TEXT DEFAULT ''")
    if "category" not in product_columns:
        add_column_if_missing("category", "ALTER TABLE products ADD COLUMN category TEXT DEFAULT ''")
    if "department" not in product_columns:
        add_column_if_missing("department", "ALTER TABLE products ADD COLUMN department TEXT DEFAULT ''")
    if "assembly_labor_min" not in product_columns:
        add_column_if_missing("assembly_labor_min", "ALTER TABLE products ADD COLUMN assembly_labor_min REAL")
    # Jobs saved before the catalog link have no SKU and no customer — they stay as they
    # are, and history renders them from their stored payload exactly as before.
    job_columns = {r[1] for r in con.execute("PRAGMA table_info(job_costs)").fetchall()}
    if "sku" not in job_columns:
        add_column_if_missing("sku", "ALTER TABLE job_costs ADD COLUMN sku TEXT DEFAULT ''")
    if "customer" not in job_columns:
        add_column_if_missing("customer", "ALTER TABLE job_costs ADD COLUMN customer TEXT DEFAULT ''")
    if "overridden" not in job_columns:
        add_column_if_missing("overridden", "ALTER TABLE job_costs ADD COLUMN overridden INTEGER NOT NULL DEFAULT 0")
    # Jobs quoted before design tracking existed carry 0 minutes and 0 fee, and
    # is_custom_job=0 — so no minimum is retroactively applied to anything you
    # already quoted. History renders exactly as it did before.
    if "design_minutes" not in job_columns:
        add_column_if_missing("design_minutes", "ALTER TABLE job_costs ADD COLUMN design_minutes REAL NOT NULL DEFAULT 0")
    if "design_fee" not in job_columns:
        add_column_if_missing("design_fee", "ALTER TABLE job_costs ADD COLUMN design_fee REAL NOT NULL DEFAULT 0")
    if "is_custom_job" not in job_columns:
        add_column_if_missing("is_custom_job", "ALTER TABLE job_costs ADD COLUMN is_custom_job INTEGER NOT NULL DEFAULT 0")
    # filament_type_id already exists here — filament.py's own init adds it as a
    # plain nullable column before this ever ran. filament_draws_json holds
    # every draw PAST THE FIRST for a part with two or more colors in one
    # print (an AMS part), exactly like a job's own multi-draw fields — the
    # first draw is the filament_type_id/filament_used_g columns themselves.
    # NULL for the overwhelming majority of parts, which have one color.
    part_columns = {r[1] for r in con.execute("PRAGMA table_info(product_parts)").fetchall()}
    if "filament_draws_json" not in part_columns:
        add_column_if_missing("filament_draws_json",
            "ALTER TABLE product_parts ADD COLUMN filament_draws_json TEXT")
    # Explicit intent, not inferred from a blank field. A linked part with
    # override=1 uses the hand-typed filament_cost even though a pool is
    # picked — leftovers, a spool bought on sale, a favor for someone. The
    # UI must show the override is active rather than silently prefer one
    # value over the other.
    if "filament_cost_override" not in part_columns:
        add_column_if_missing("filament_cost_override",
            "ALTER TABLE product_parts ADD COLUMN filament_cost_override INTEGER NOT NULL DEFAULT 0")
    # ── Retrofit FOREIGN KEYs onto product_parts ──────────────────────────────
    # product_parts existed for a while with no FOREIGN KEY at all, and later
    # picked up a plain filament_type_id column (added by filament.py, no
    # constraint). SQLite has no ALTER TABLE ADD CONSTRAINT, so the only way
    # to add either one now is to recreate the table — safe here only because
    # it is currently empty. If a future run of this file ever finds real
    # rows AND a missing constraint, it leaves the table alone rather than
    # risk a swap against live BOM data; a missing FK is a smaller problem
    # than a botched table rebuild.
    parts_fks = {r[3] for r in con.execute(
        "PRAGMA foreign_key_list(product_parts)").fetchall()}
    if "sku" not in parts_fks or "filament_type_id" not in parts_fks:
        parts_count = con.execute("SELECT COUNT(*) FROM product_parts").fetchone()[0]
        if parts_count == 0:
            con.executescript("""
                DROP TABLE product_parts;
                CREATE TABLE product_parts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sku TEXT NOT NULL,
                    part_name TEXT NOT NULL DEFAULT 'Main',
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    qty_per_product REAL NOT NULL DEFAULT 1,
                    qty_on_plate REAL,
                    filament_used_g REAL,
                    filament_cost REAL,
                    filament_type_id INTEGER,
                    filament_draws_json TEXT,
                    filament_cost_override INTEGER NOT NULL DEFAULT 0,
                    print_time_text TEXT DEFAULT '',
                    active_labor_min REAL,
                    material TEXT DEFAULT '',
                    nozzle_size REAL,
                    layer_height REAL,
                    stl_filename TEXT DEFAULT '',
                    threemf_filename TEXT DEFAULT '',
                    notes TEXT DEFAULT '',
                    FOREIGN KEY (sku) REFERENCES products(sku) ON DELETE RESTRICT,
                    FOREIGN KEY (filament_type_id) REFERENCES filament_types(id) ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS idx_product_parts_sku ON product_parts(sku);
                CREATE INDEX IF NOT EXISTS idx_product_parts_filament_type ON product_parts(filament_type_id);
            """)
    # ── Seed rows and the 3DP-* SKU migration were removed on 2026-08-29 ──────
    # Both were one-time fixtures. The seeds re-inserted four empty products on every
    # container start, which is how the catalog ended up holding an archived row with
    # the real cost data beside an active row with none: the seed claimed the new SKU
    # first, so the rename that was supposed to carry the data across saw the name taken
    # and silently skipped. Nothing re-creates products now — the catalog only ever
    # contains what you put in it.
    # ── BOM backfill ──────────────────────────────────────────────────────────
    # Every product costed before the bill-of-materials rework kept its print
    # parameters directly on the products row. Move each of those into a single
    # "Main" part so existing costs come out to exactly the same number. The
    # legacy columns are left in place, unread, so a rollback loses nothing.
    # Guarded with NOT EXISTS so two gunicorn workers starting together can't
    # both seed the same product.
    con.execute("""
        INSERT INTO product_parts
            (sku, part_name, sort_order, qty_per_product, qty_on_plate,
             filament_used_g, filament_cost, print_time_text, active_labor_min,
             material, nozzle_size, layer_height, stl_filename, threemf_filename)
        SELECT p.sku, 'Main', 0, 1, p.qty_on_plate,
               p.filament_used_g, p.filament_cost, COALESCE(p.print_time_text,''),
               p.active_labor_min, COALESCE(p.material,''), p.nozzle_size,
               p.layer_height, COALESCE(p.stl_filename,''), COALESCE(p.threemf_filename,'')
        FROM products p
        WHERE p.qty_on_plate IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM product_parts pp WHERE pp.sku = p.sku)
    """)
    for k, v in DEFAULT_SETTINGS.items():
        con.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
    con.commit()
    con.close()


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(DB_PATH.parent / ".schema.lock"), timeout=60):
        _init_db()
