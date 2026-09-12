from io import BytesIO
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.graphics.barcode import qr
from reportlab.graphics.shapes import Drawing
from reportlab.graphics import renderPDF
from config import *
def mm_to_in(mm):
    return float(mm) / 25.4


def fit_text(text, font_name, max_size, min_size, max_width_pt):
    size = max_size
    while size > min_size and stringWidth(text, font_name, size) > max_width_pt:
        size -= 0.25
    return max(size, min_size)


def draw_qr(c, value, x, y, size_pt):
    widget = qr.QrCodeWidget(value)
    bounds = widget.getBounds()
    w = bounds[2] - bounds[0]
    h = bounds[3] - bounds[1]
    d = Drawing(size_pt, size_pt, transform=[size_pt/w, 0, 0, size_pt/h, 0, 0])
    d.add(widget)
    renderPDF.draw(d, c, x, y)


def draw_label(c, x_in, y_top_in, item, settings):
    x = x_in * 72
    y_top = (PAGE_H_IN - y_top_in) * 72
    w = LABEL_W_IN * 72
    pad = 0.10 * 72
    show_qr = settings.get("show_qr") == "1"
    show_sku = settings.get("show_sku") == "1"
    show_qty = settings.get("show_qty") == "1"
    company = settings.get("company_name", "").strip()
    content_left = x + pad
    content_right = x + w - pad
    qr_size = 0
    if show_qr:
        qr_size = 0.60 * 72
        value = (settings.get("qr_prefix") or "") + item["sku"]
        draw_qr(c, value, x + w - qr_size - 0.09*72, y_top - 0.80*72, qr_size)
        content_right -= qr_size + 0.06*72
    usable_w = content_right - content_left
    center_x = content_left + usable_w/2
    y = y_top - 0.20*72
    if company:
        c.setFont("Helvetica", 6.5)
        c.drawCentredString(center_x, y, company)
        y -= 0.14*72
    product_size = fit_text(item["product_name"], "Helvetica-Bold", 10.5, 7.2, usable_w)
    c.setFont("Helvetica-Bold", product_size)
    c.drawCentredString(center_x, y, item["product_name"])
    y -= 0.25*72
    if show_sku:
        c.setFont("Helvetica", 8.0)
        # Department left the SKU string itself (see SKU_RE) but "who am I
        # printing this for" still has to be visible at a glance on the
        # physical label — it just rides on the same line and the same
        # show_sku toggle now, as a prefix, instead of being baked into the
        # SKU. .get(), not [] -- calibration labels carry no department key
        # at all and must not break.
        sku_line = f"SKU: {item['sku']}"
        department = item.get("department")
        if department:
            sku_line = f"{department} · {sku_line}"
        c.drawCentredString(center_x, y, sku_line)
        y -= 0.22*72
    if show_qty:
        c.setFont("Helvetica-Bold", 9.5)
        c.drawCentredString(center_x, y, f"Qty: {item['qty']}")


def make_pdf(labels, start_position, settings):
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    x_off = mm_to_in(settings.get("x_offset_mm", "0") or 0)
    y_off = mm_to_in(settings.get("y_offset_mm", "0") or 0)
    absolute_index = max(0, start_position - 1)
    for item in labels:
        pos = absolute_index % LABELS_PER_PAGE
        if absolute_index > 0 and pos == 0:
            c.showPage()
        row = pos // COLS
        col = pos % COLS
        x = LEFT_MARGIN_IN + col * (LABEL_W_IN + H_GAP_IN) + x_off
        y_top = TOP_MARGIN_IN + row * LABEL_H_IN + y_off
        draw_label(c, x, y_top, item, settings)
        absolute_index += 1
    c.save()
    buf.seek(0)
    return buf


def make_job_cost_pdf(job_label, created_at, detail):
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    page_w, page_h = letter
    y = page_h - 0.75 * 72
    left = 0.75 * 72
    def line(text, size=10, bold=False, gap=0.22):
        nonlocal y
        font = "Helvetica-Bold" if bold else "Helvetica"
        chunks, current = [], ""
        for char in str(text):
            if stringWidth(current + char, font, size) > page_w - 2 * left:
                chunks.append(current); current = ""
            current += char
        chunks.append(current)
        for chunk in chunks:
            if y < 0.65 * 72:
                c.showPage(); y = page_h - 0.75 * 72
            c.setFont(font, size)
            c.drawString(left, y, chunk)
            y -= gap * 72
    line("Job Cost Summary", size=16, bold=True, gap=0.32)
    line(f"Job: {job_label}", size=12, bold=True)
    if detail.get("customer"):
        line(f"Customer: {detail['customer']}", size=11)
    if detail.get("sku"):
        line(f"Product: {detail['sku']} — {detail.get('product_name','')}", size=11)
    line(f"Created: {created_at}", size=9)
    if detail.get("overridden"):
        line("Costed with per-job overrides — figures below differ from the catalog.", size=9)
    y -= 0.12 * 72
    line("Inputs", size=11, bold=True)
    line(f"Order Quantity: {detail.get('order_quantity','')}")
    line(f"Selling Price / Item: ${detail.get('selling_price_per_item','')}")
    line(f"Sales Tax Rate: {detail.get('sales_tax_rate','')}%")
    line(f"Other Job Cost: ${detail.get('other_job_cost','')}")
    snapshot = detail.get("parts_snapshot") or []
    if snapshot:
        for p in snapshot:
            line(f"  {p.get('part_name','Part')}: {p.get('qty_on_plate','')} on plate, "
                 f"{p.get('print_time_text','')}, {p.get('filament_used_g','') or 0} g, "
                 f"${p.get('filament_cost','') or 0}, x{p.get('qty_per_product',1)} per item", size=9)
    else:
        line(f"Qty on Plate: {detail.get('qty_on_plate','')}")
        line(f"Filament Used (g): {detail.get('filament_used_g','')}")
        line(f"Filament Cost ($): {detail.get('filament_cost','')}")
        line(f"Print Time: {detail.get('print_time_text','')}")
        line(f"Active Labor (min): {detail.get('active_labor_min','') or 'Default'}")
    y -= 0.12 * 72
    r = detail.get("result", {})
    line("Job Analysis", size=11, bold=True)
    if r.get("parts_plan"):
        for p in r["parts_plan"]:
            # Jobs saved before this per-unit breakdown existed have no
            # cost_per_unit in their stored payload_json — frozen historical
            # snapshot, never recomputed. .get() degrades to the original
            # total line instead of a KeyError on an old record.
            cpu = p.get("cost_per_unit")
            cpu_needed = p.get("cost_per_unit_needed")
            batch_line = (f"{p['units_produced']:g} @ ${cpu:.3f} = ${p['part_total_cost']:.2f}"
                          if cpu is not None else f"${p['part_total_cost']:.2f}")
            needed_line = f", ${cpu_needed:.3f}/unit needed" if cpu_needed is not None else ""
            line(f"  {p['part_name']}: need {p['units_needed']:g} -> {p['print_runs_required']} runs "
                 f"({p['units_produced']:g} made, {p['extra_units']:g} spare) = {batch_line}{needed_line}", size=9)
        line(f"Total Print Time: {r.get('total_print_hours',0):.1f} h")
        if r.get("filament_total_g"):
            line(f"Filament Consumed: {r['filament_total_g']:,.0f} g")
        if r.get("assembly_total"):
            line(f"Assembly Labor Total: ${r['assembly_total']:.2f}")
    else:
        line(f"Print Runs Required: {r.get('print_runs_required','')}")
        line(f"Total Items Produced: {r.get('total_items_produced','')}")
    line(f"Total Production Cost: ${r.get('total_production_cost','')}")
    line(f"Total True Job Cost: ${r.get('total_true_job_cost','')}")
    line(f"Sales Subtotal: ${r.get('sales_subtotal','')}")
    # Older quotes predate design tracking and carry none of these keys — they
    # print exactly as they always did.
    if r.get("design_fee"):
        line(f"Design & Setup ({r.get('design_minutes',0):,.0f} min "
             f"@ ${r.get('design_rate',0):.2f}/hr): ${r['design_fee']:.2f}", bold=True)
    if r.get("min_job_charge_applied"):
        line(f"Minimum job charge applied — quote raised from "
             f"${r.get('charge_before_min',0):.2f} to ${r.get('job_charge',0):.2f}", size=9)
    line(f"Sales Tax: ${r.get('sales_tax','')}")
    line(f"Invoice Total: ${r.get('invoice_total','')}")
    line(f"Gross Profit: ${r.get('gross_profit','')}")
    line(f"Profit / Item: ${round(r.get('profit_per_item',0),2)}")
    margin = r.get("actual_margin")
    line(f"Actual Margin: {round(margin*100,1) if margin is not None else '—'}%")
    line(f"Break-Even Price / Item: ${round(r.get('break_even_price',0),2)}")
    c.save()
    buf.seek(0)
    return buf
