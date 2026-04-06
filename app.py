import streamlit as st
import anthropic
import base64
import json
import tempfile
import os
from docx import Document
from docx.shared import Pt, RGBColor, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
import fitz  # PyMuPDF

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Poetry OCR Extractor",
    page_icon="📖",
    layout="wide",
)

st.title("📖 Poetry OCR Extractor")
st.markdown(
    "Sube un PDF con poemas. Claude analiza cada página, detecta títulos, "
    "hablantes y versos, y genera un documento Word limpio."
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def pdf_page_to_base64(pdf_path: str, page_index: int) -> str:
    """Render a single PDF page to PNG at 2x zoom and return base64."""
    doc = fitz.open(pdf_path)
    page = doc[page_index]
    mat = fitz.Matrix(2.0, 2.0)   # ~144 dpi
    pix = page.get_pixmap(matrix=mat, alpha=False)
    png_bytes = pix.tobytes("png")
    doc.close()
    return base64.standard_b64encode(png_bytes).decode()


def is_blank_page(pdf_path: str, page_index: int, threshold: float = 0.98) -> bool:
    """Return True if the page is visually blank (almost all white pixels)."""
    doc = fitz.open(pdf_path)
    page = doc[page_index]
    mat = fitz.Matrix(0.5, 0.5)   # low-res for speed
    pix = page.get_pixmap(matrix=mat, alpha=False, colorspace=fitz.csGRAY)
    samples = pix.samples
    doc.close()
    white = sum(1 for b in samples if b > 240)
    return (white / len(samples)) >= threshold


# ── Claude prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Eres un experto en OCR y transcripción de poesía hispanohablante.
Recibirás la imagen de una página de un libro de poemas.

Tu tarea es extraer TODO el contenido estructurado visible en la página.

Devuelve ÚNICAMENTE un objeto JSON válido con esta forma exacta
(sin bloques de código, sin texto antes o después):

{
  "blank": false,
  "page_header": "texto del encabezado de página si existe, o null",
  "poems": [
    {
      "title": "TÍTULO DEL POEMA en mayúsculas tal como aparece, o null",
      "sections": [
        {
          "speaker": "Nombre del hablante si aparece (ej: 'El paciente:', 'El médico:'), o null",
          "lines": ["verso 1", "verso 2", "..."]
        }
      ]
    }
  ],
  "footnotes": ["nota al pie 1", "nota al pie 2"]
}

Reglas CRÍTICAS:
1. Si la página está completamente en blanco, devuelve {"blank": true, "page_header": null, "poems": [], "footnotes": []}.
2. Una sola página puede contener DOS o más poemas completos — inclúyelos todos en el array "poems".
3. Un poema puede tener múltiples secciones con distintos hablantes.
   Ejemplo: El poema "El mal del siglo" tiene sección "El paciente:" y sección "El médico:".
4. Si no hay hablante en una sección, pon null en "speaker".
5. Transcribe los versos exactamente: conserva tildes, puntos suspensivos, signos de exclamación/interrogación, mayúsculas.
6. Las notas al pie (generalmente en letra pequeña al fondo) van en "footnotes" (puede ser []).
7. Ignora los números de página y los números de línea que aparezcan a la izquierda de los versos.
8. No incluyas las variantes textuales que aparecen a la derecha de los versos (son anotaciones editoriales).
"""


def extract_page(client: anthropic.Anthropic, b64_image: str, page_num: int) -> dict:
    """Call Claude vision to extract structured content from one page image."""
    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": b64_image,
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            f"Extrae el contenido estructurado de esta página (página {page_num}). "
                            "Recuerda: puede haber 0, 1 o 2 poemas en la misma página. "
                            "Devuelve solo el JSON."
                        ),
                    },
                ],
            }
        ],
    )
    raw = message.content[0].text.strip()
    # Strip markdown fences if model wraps the JSON
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


# ── Word document builder ─────────────────────────────────────────────────────

def add_page_break(doc: Document) -> None:
    p = doc.add_paragraph()
    run = p.add_run()
    br = OxmlElement("w:br")
    br.set(qn("w:type"), "page")
    run._r.append(br)


def build_docx(all_pages: list) -> bytes:
    doc = Document()

    # Default font
    style = doc.styles["Normal"]
    style.font.name = "Garamond"
    style.font.size = Pt(11)

    seen_titles: set = set()
    first_content = True

    for page_data in all_pages:
        # Blank page → page break in Word
        if page_data.get("blank"):
            if not first_content:
                add_page_break(doc)
            else:
                doc.add_paragraph()
            continue

        poems = page_data.get("poems", [])
        footnotes = page_data.get("footnotes", [])

        if not poems and not footnotes:
            continue

        first_content = False

        for poem in poems:
            title = poem.get("title") or ""
            title_key = title.strip().upper()

            # Title — only once per poem even if it spans multiple pages
            if title_key and title_key not in seen_titles:
                seen_titles.add(title_key)
                p = doc.add_paragraph()
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                run = p.add_run(title.strip())
                run.bold = True
                run.font.size = Pt(13)
                p.paragraph_format.space_before = Pt(20)
                p.paragraph_format.space_after = Pt(8)

            # Sections
            for section in poem.get("sections", []):
                speaker = section.get("speaker")
                if speaker:
                    sp = doc.add_paragraph()
                    sr = sp.add_run(speaker.strip())
                    sr.italic = True
                    sr.font.size = Pt(11)
                    sp.paragraph_format.space_before = Pt(8)
                    sp.paragraph_format.space_after = Pt(2)

                for line in section.get("lines", []):
                    lp = doc.add_paragraph()
                    lp.add_run(line)
                    lp.paragraph_format.space_before = Pt(0)
                    lp.paragraph_format.space_after = Pt(1)
                    lp.paragraph_format.left_indent = Inches(0.5)

            # Gap between poems on the same page
            gap = doc.add_paragraph()
            gap.paragraph_format.space_after = Pt(10)

        # Footnotes
        for fn in footnotes:
            fp = doc.add_paragraph()
            fr = fp.add_run(fn.strip())
            fr.font.size = Pt(8.5)
            fr.font.color.rgb = RGBColor(0x55, 0x55, 0x55)
            fp.paragraph_format.space_before = Pt(0)
            fp.paragraph_format.space_after = Pt(1)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".docx") as tmp:
        doc.save(tmp.name)
        tmp_path = tmp.name

    with open(tmp_path, "rb") as f:
        data = f.read()
    os.unlink(tmp_path)
    return data


# ── Page range parser ─────────────────────────────────────────────────────────

def parse_page_range(spec: str, total: int) -> list:
    if spec.strip().lower() in ("todas", "all", ""):
        return list(range(total))
    indices = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            indices.update(range(int(a) - 1, int(b)))
        elif part.isdigit():
            indices.add(int(part) - 1)
    return sorted(i for i in indices if 0 <= i < total)


# ── UI ────────────────────────────────────────────────────────────────────────

st.info(
    "💡 **API Key:** Cópiala desde [console.anthropic.com](https://console.anthropic.com) "
    "→ *API Keys*. Asegúrate de que no tenga espacios al inicio o al final.",
)

api_key = st.text_input(
    "🔑 Anthropic API Key",
    type="password",
    placeholder="sk-ant-api03-...",
    help="No se almacena en ningún servidor.",
)

uploaded_file = st.file_uploader("📄 Sube tu PDF", type=["pdf"])

col1, col2 = st.columns([1, 2])
with col1:
    page_range = st.text_input(
        "Páginas a procesar",
        value="todas",
        placeholder="todas  ó  1-10  ó  1,3,5-8",
    )
with col2:
    detect_blanks = st.checkbox(
        "Detectar páginas en blanco automáticamente",
        value=True,
        help="Evita llamadas innecesarias a Claude para páginas vacías.",
    )

if st.button("🚀 Procesar PDF", disabled=not (api_key and uploaded_file), type="primary"):

    api_key = api_key.strip()
    if not api_key.startswith("sk-ant-"):
        st.error(
            "⛔ La API key no parece válida — debe comenzar con `sk-ant-`. "
            "Cópiala desde [console.anthropic.com](https://console.anthropic.com)."
        )
        st.stop()

    client = anthropic.Anthropic(api_key=api_key)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_pdf:
        tmp_pdf.write(uploaded_file.read())
        pdf_path = tmp_pdf.name

    pdf_doc = fitz.open(pdf_path)
    total_pages = len(pdf_doc)
    pdf_doc.close()

    pages_to_process = parse_page_range(page_range, total_pages)
    st.info(f"Procesando {len(pages_to_process)} de {total_pages} páginas…")

    progress = st.progress(0)
    status = st.empty()
    all_pages = []
    errors = []
    blank_count = 0

    for i, page_idx in enumerate(pages_to_process):
        page_num = page_idx + 1
        status.text(f"Analizando página {page_num} / {len(pages_to_process)}…")

        try:
            if detect_blanks and is_blank_page(pdf_path, page_idx):
                all_pages.append({
                    "_page": page_num,
                    "blank": True,
                    "page_header": None,
                    "poems": [],
                    "footnotes": [],
                })
                blank_count += 1
            else:
                b64 = pdf_page_to_base64(pdf_path, page_idx)
                result = extract_page(client, b64, page_num)
                result["_page"] = page_num
                all_pages.append(result)

        except json.JSONDecodeError as e:
            errors.append(f"Página {page_num}: JSON inválido — {e}")
        except Exception as e:
            errors.append(f"Página {page_num}: {e}")

        progress.progress((i + 1) / len(pages_to_process))

    os.unlink(pdf_path)
    progress.empty()
    status.empty()

    if errors:
        with st.expander(f"⚠️ {len(errors)} errores — click para ver"):
            for err in errors:
                st.code(err)

    if all_pages:
        ok = len(all_pages) - len(errors)
        st.success(
            f"✅ {ok} páginas procesadas · {blank_count} en blanco preservadas."
        )

        with st.expander("🔍 Ver datos extraídos (JSON)"):
            st.json(all_pages)

        with st.spinner("Generando documento Word…"):
            docx_bytes = build_docx(all_pages)

        st.download_button(
            label="⬇️ Descargar documento Word",
            data=docx_bytes,
            file_name="poemas_extraidos.docx",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    else:
        st.error("No se pudo extraer contenido de ninguna página.")
