import os
from pathlib import Path
APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("TALLY_DATA_DIR", str(APP_DIR / "data"))).resolve()
DB_PATH = DATA_DIR / "labels.db"
PHOTO_DIR = DATA_DIR / "product-photos"
# Exact geometry for 30-up 2.625" x 1" address label sheets (US Letter).
# Compatible with Avery(R) 5160 / 8160 and the many generic equivalents.
PAGE_W_IN = 8.5
PAGE_H_IN = 11.0
LABEL_W_IN = 2.625
LABEL_H_IN = 1.0
LEFT_MARGIN_IN = 0.1875
TOP_MARGIN_IN = 0.5
H_GAP_IN = 0.125
COLS = 3
ROWS = 10
LABELS_PER_PAGE = 30
DEFAULT_ACTIVE_LABOR_MIN = 2.0  # Used when a product's Active Labor field is left blank.
                                # Not exposed in Settings; change here if it needs to move.
DEFAULT_SETTINGS = {
    "x_offset_mm": "0",
    "y_offset_mm": "0",
    "show_sku": "1",
    "show_qty": "1",
    "show_qr": "0",
    "qr_prefix": "",
    "company_name": "",
    "labor_rate": "25",
    "electricity_rate": "0.15",
    "printer_watts": "120",
    "machine_wear_rate": "0.1",
    "default_wholesale_margin": "0.6",
    "default_retail_margin": "0.7",
    "scrap_rate": "10",         # % of prints expected to fail. 0 = off. See the one-plate
                                # rule in calculate_job_cost_from_parts before changing this.
    "sales_tax_rate": "0",      # % — your state rate, pre-filled on the Job Calculator.
    "default_active_labor_min": "2",
    # ── Custom design work ────────────────────────────────────────────────
    # design_rate is NOT labor_rate. labor_rate ($25) is machine-tending time —
    # pulling parts off a plate, deburring, bagging — and it recurs on every print
    # run. design_rate is one-time CAD/setup time that happens once per design and
    # never again on a reorder, which is exactly why it must never be entered into
    # a part's active_labor_min: that field is multiplied by every run, forever.
    "design_rate": "50",
    # Floor for a TRUE custom design job only (is_custom_job). Never applies to a
    # personalised catalogue product — a cake topper with different text is a SKU
    # with a text field, not a design job, and gets no minimum and no design fee.
    "min_job_charge": "40",
}
