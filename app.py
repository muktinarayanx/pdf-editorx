import os
import re
import random
import uuid
from datetime import datetime, timedelta
from flask import Flask, request, send_file, render_template, jsonify
import fitz  # PyMuPDF

app = Flask(__name__, template_folder='.')
UPLOAD_FOLDER = "uploads"
OUTPUT_FOLDER = "outputs"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# Regex to match dates like: 22 Feb 2026 07:10:51 PM  or  22 Feb 2025 07:22:41 PM
DATE_PATTERN = re.compile(
    r"(\d{1,2})\s+([A-Z][a-z]{2})\s+(\d{4})\s+(\d{1,2}):(\d{2}):(\d{2})\s+(AM|PM)"
)

# Regex to match Chalan / Pass No. — standalone 18–22 digit numbers
CHALAN_PATTERN = re.compile(r"(?<![\d])\d{18,22}(?![\d])")


def random_chalan():
    """Generate a unique random 20-digit number (first digit non-zero)."""
    return str("99" + "".join([str(random.randint(0, 9)) for _ in range(18)]))

MONTH_MAP = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12
}
MONTH_NAMES = {v: k for k, v in MONTH_MAP.items()}


def parse_date(text):
    m = DATE_PATTERN.search(text)
    if not m:
        return None
    day, mon, year, hour, minute, second, ampm = m.groups()
    month_num = MONTH_MAP.get(mon)
    if not month_num:
        return None
    hour = int(hour)
    minute = int(minute)
    second = int(second)
    year = int(year)
    day = int(day)
    if ampm == "PM" and hour != 12:
        hour += 12
    elif ampm == "AM" and hour == 12:
        hour = 0
    try:
        dt = datetime(year, month_num, day, hour, minute, second)
    except ValueError:
        return None
    return dt, m.group(0)


def advance_date(dt):
    """Advance by exactly 1 day. Keep the same hour (AM/PM unchanged).
    Randomize minutes (0-59) and seconds (0-59) independently."""
    new_dt = dt + timedelta(days=1)
    new_dt = new_dt.replace(minute=random.randint(0, 59), second=random.randint(0, 59))
    return new_dt


def format_date(dt):
    hour = dt.hour
    minute = dt.minute
    second = dt.second
    day = dt.day
    month = MONTH_NAMES[dt.month]
    year = dt.year
    ampm = "AM" if hour < 12 else "PM"
    hour12 = hour % 12
    if hour12 == 0:
        hour12 = 12
    return f"{day:02d} {month} {year} {hour12:02d}:{minute:02d}:{second:02d} {ampm}"


def process_pdf(input_path, output_path):
    doc = fitz.open(input_path)
    total_replacements = 0
    print_day_str = None  # will be set from page 1's print date new datetime

    for page_num in range(len(doc)):
        page = doc[page_num]
        blocks = page.get_text("dict")["blocks"]

        # ── Phase 1: Collect every span on the page ──────────────────────────
        all_spans = []
        for block in blocks:
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    all_spans.append((span, line.get("dir", (1, 0))))

        # ── Phase 2: Detect date spans and classify base vs. expiry (+6h) ────
        # date_info: list of {idx, dt, span, dir}
        date_info = []
        for idx, (span, direction) in enumerate(all_spans):
            result = parse_date(span["text"])
            if result:
                dt, _ = result
                date_info.append({"idx": idx, "dt": dt, "span": span, "dir": direction})

        # Mark which date spans are "expiry" (exactly 6 h after a base span)
        expiry_ids = set()          # idx of spans that are row-9 expiry dates
        base_to_expiry_dt = {}      # base_span_idx -> base_new_dt (filled below)

        TOLERANCE = 65              # seconds tolerance when checking 6-hour gap
        for i, a in enumerate(date_info):
            for j, b in enumerate(date_info):
                if i == j:
                    continue
                diff = (b["dt"] - a["dt"]).total_seconds()
                if abs(diff - 6 * 3600) <= TOLERANCE:
                    expiry_ids.add(b["idx"])  # b is the expiry date of a

        # ── Phase 3: Compute new datetimes ───────────────────────────────────
        # Rules:
        #   • Print date and row 8 share the same original datetime
        #     → ONE shared advance_date() call so they get identical min/sec
        #   • Row 9 = that shared row-8 new_dt + exactly 6 hours

        new_dt_map = {}   # span_idx -> new datetime
        dt_to_new_dt = {} # original_dt -> shared new datetime
        TOLERANCE = 65    # seconds tolerance for 6-hour gap detection

        # Group base spans by original datetime — same dt = same new_dt
        for info in date_info:
            if info["idx"] not in expiry_ids:
                orig_dt = info["dt"]
                if orig_dt not in dt_to_new_dt:
                    dt_to_new_dt[orig_dt] = advance_date(orig_dt)
                new_dt_map[info["idx"]] = dt_to_new_dt[orig_dt]

        # Row 9 links ONLY to the row 8 span (origin x >= 100), not print date
        for i, a in enumerate(date_info):
            if a["idx"] not in new_dt_map:
                continue
            origin_a = a["span"].get("origin")
            ox_a = origin_a[0] if origin_a else 999
            if ox_a < 100:
                continue  # print date never drives row 9
            for j, b in enumerate(date_info):
                if i == j:
                    continue
                diff = (b["dt"] - a["dt"]).total_seconds()
                if abs(diff - 6 * 3600) <= TOLERANCE:
                    new_dt_map[b["idx"]] = new_dt_map[a["idx"]] + timedelta(hours=6)


        # ── Phase 4: Capture print day from page 1 for filename ──────────────
        if page_num == 0 and print_day_str is None:
            for info in date_info:
                if info["idx"] not in expiry_ids:
                    origin_x = info["span"].get("origin", (999, 0))[0]
                    if origin_x < 100 and info["idx"] in new_dt_map:
                        new_print_dt = new_dt_map[info["idx"]]
                        print_day_str = str(new_print_dt.day)  # e.g. "23"
                        break

        # ── Phase 5 (was 4): Build replacement list (dates + chalan numbers) ──
        replacements = []

        for span, direction in all_spans:
            span_text = span["text"]
            new_text = span_text
            span_idx = next(
                (info["idx"] for info in date_info if info["span"] is span),
                None
            )

            # Date replacement
            result = parse_date(span_text)
            if result:
                old_dt, matched_str = result
                if span_idx is not None and span_idx in new_dt_map:
                    new_text = new_text.replace(matched_str, format_date(new_dt_map[span_idx]))

            # Chalan / Pass No. replacement (18-22 digit numbers)
            def replace_chalan(m):
                return random_chalan()
            new_text = CHALAN_PATTERN.sub(replace_chalan, new_text)

            if new_text != span_text:
                replacements.append({
                    "old": span_text,
                    "new": new_text,
                    "rect": fitz.Rect(span["bbox"]),
                    "font": span.get("font", "helv"),
                    "size": span.get("size", 10),
                    "color": span.get("color", 0),
                    "origin": span.get("origin"),
                    "dir": direction,
                })

        # ── Phase 5: Redact old text ──────────────────────────────────────────
        for rep in replacements:
            page.add_redact_annot(rep["rect"], fill=(1, 1, 1))
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

        # ── Phase 6: Insert new text using built-in PDF fonts ─────────────────
        for rep in replacements:
            color_int = rep["color"]
            r = ((color_int >> 16) & 0xFF) / 255.0
            g = ((color_int >> 8) & 0xFF) / 255.0
            b = (color_int & 0xFF) / 255.0

            font_name = rep["font"]
            if "Bold" in font_name:
                builtin = "hebo"
            elif "Italic" in font_name or "Oblique" in font_name:
                builtin = "heit"
            else:
                builtin = "helv"

            dx, dy = rep["dir"]
            is_vertical = abs(dy) > abs(dx)
            angle = (90 if dy < 0 else -90) if is_vertical else 0

            if rep["origin"]:
                page.insert_text(
                    fitz.Point(rep["origin"]),
                    rep["new"],
                    fontsize=rep["size"],
                    fontname=builtin,
                    color=(r, g, b),
                    rotate=angle,
                )
            else:
                page.insert_textbox(
                    rep["rect"],
                    rep["new"],
                    fontsize=rep["size"],
                    fontname=builtin,
                    color=(r, g, b),
                    rotate=angle if is_vertical else 0,
                )
            total_replacements += 1



    # Save with maximum compression:
    # garbage=4  → remove all unused objects + deduplicate streams (kills bloated font copies)
    # deflate=True → compress all streams with zlib (like gzip)
    # deflate_images=True → also compress image streams
    # clean=True  → sanitize and deduplicate content streams
    doc.save(
        output_path,
        garbage=4,
        deflate=True,
        deflate_images=True,
        clean=True,
    )
    doc.close()
    return total_replacements, print_day_str




@app.route("/")
def index():
    return render_template('index.html')


@app.route("/process", methods=["POST"])
def process():
    if "pdf_file" not in request.files:
        return jsonify({"success": False, "error": "No file uploaded."})

    file = request.files["pdf_file"]
    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"success": False, "error": "Only PDF files are supported."})

    file_id = str(uuid.uuid4())
    input_path = os.path.join(UPLOAD_FOLDER, f"{file_id}_input.pdf")
    output_filename = file.filename.replace(".pdf", "_modified.pdf")
    output_path = os.path.join(OUTPUT_FOLDER, f"{file_id}_output.pdf")

    file.save(input_path)

    try:
        doc = fitz.open(input_path)
        num_pages = len(doc)
        doc.close()

        replacements = process_pdf(input_path, output_path)
        # build filename from print date day: All_23.pdf
        file_day = replacements[1] if isinstance(replacements, tuple) else None
        count = replacements[0] if isinstance(replacements, tuple) else replacements
        output_filename = f"All_{file_day}.pdf" if file_day else "All_modified.pdf"
        return jsonify({
            "success": True,
            "file_id": file_id,
            "filename": output_filename,
            "replacements": count,
            "pages": num_pages,
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})
    finally:
        if os.path.exists(input_path):
            os.remove(input_path)


@app.route("/download/<file_id>")
def download(file_id):
    # Sanitize file_id — only allow UUID-like strings
    if not re.fullmatch(r"[0-9a-f\-]{36}", file_id):
        return "Invalid file ID", 400
    output_path = os.path.join(OUTPUT_FOLDER, f"{file_id}_output.pdf")
    if not os.path.exists(output_path):
        return "File not found or expired.", 404
    fname = request.args.get("fname", "All_modified.pdf")
    # Basic sanitize — strip path separators
    fname = fname.replace("/", "").replace("\\", "") or "All_modified.pdf"
    # Send file then delete it — prevents disk accumulation on Render
    response = send_file(output_path, as_attachment=True, download_name=fname)
    @response.call_on_close
    def cleanup():
        try:
            os.remove(output_path)
        except OSError:
            pass
    return response


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5500))
    app.run(host="0.0.0.0", port=port, debug=False)
