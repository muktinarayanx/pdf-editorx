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
    return str(random.randint(10, 99)) + "".join([str(random.randint(0, 9)) for _ in range(18)])

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
    """Advance by 1 day and shift time by ±10 minutes randomly."""
    delta_minutes = random.randint(-10, 10)
    new_dt = dt + timedelta(days=1, minutes=delta_minutes)
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

    for page_num in range(len(doc)):
        page = doc[page_num]
        # Get all text spans with their bounding boxes
        blocks = page.get_text("dict")["blocks"]

        replacements = []  # (old_text, new_text, rect, font_name, font_size, color, flags, origin, direction)

        for block in blocks:
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    span_text = span["text"]
                    new_text = span_text

                    # --- Date replacement ---
                    result = parse_date(span_text)
                    if result:
                        old_dt, matched_str = result
                        new_dt = advance_date(old_dt)
                        new_text = new_text.replace(matched_str, format_date(new_dt))

                    # --- Chalan / Pass No. replacement (18-22 digit numbers) ---
                    def replace_chalan(m):
                        return random_chalan()
                    new_text = CHALAN_PATTERN.sub(replace_chalan, new_text)

                    if new_text != span_text:
                        rect = fitz.Rect(span["bbox"])
                        font_name = span.get("font", "helv")
                        font_size = span.get("size", 10)
                        color = span.get("color", 0)
                        flags = span.get("flags", 0)
                        replacements.append((
                            span_text, new_text, rect,
                            font_name, font_size, color, flags,
                            span.get("origin"), line.get("dir", (1, 0))
                        ))

        for (old_text, new_text, rect, font_name, font_size, color, flags, origin, direction) in replacements:
            # Redact the old text area
            page.add_redact_annot(rect, fill=(1, 1, 1))  # white fill
        page.apply_redactions()

        # Re-add new text in same positions
        for (old_text, new_text, rect, font_name, font_size, color, flags, origin, direction) in replacements:
            # Decode color from int to RGB tuple
            r = ((color >> 16) & 0xFF) / 255.0
            g = ((color >> 8) & 0xFF) / 255.0
            b = (color & 0xFF) / 255.0

            # Determine if text is rotated (vertical)
            # direction is a tuple like (cos, sin) of text baseline direction
            dx, dy = direction
            is_vertical = abs(dy) > abs(dx)

            # Use insertText with rotation if needed
            if is_vertical:
                # Text runs bottom-to-top (dy < 0) or top-to-bottom (dy > 0)
                angle = 90 if dy < 0 else -90
            else:
                angle = 0

            # Try to match font
            simple_font = "helv"
            if "Bold" in font_name:
                simple_font = "hebo"
            elif "Italic" in font_name or "Oblique" in font_name:
                simple_font = "heit"

            # Insert text at original origin point
            if origin:
                page.insert_text(
                    fitz.Point(origin),
                    new_text,
                    fontsize=font_size,
                    fontname=simple_font,
                    color=(r, g, b),
                    rotate=angle,
                )
            else:
                page.insert_textbox(
                    rect,
                    new_text,
                    fontsize=font_size,
                    fontname=simple_font,
                    color=(r, g, b),
                    rotate=angle if is_vertical else 0,
                )
            total_replacements += 1

    doc.save(output_path)
    doc.close()
    return total_replacements


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
        return jsonify({
            "success": True,
            "file_id": file_id,
            "filename": output_filename,
            "replacements": replacements,
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
    return send_file(output_path, as_attachment=True, download_name="modified.pdf")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5500))
    app.run(host="0.0.0.0", port=port, debug=False)
