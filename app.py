import streamlit as st
import anthropic
import base64
import json
import tempfile
import os
from pathlib import Path
from docx import Document
from docx.shared import Pt, RGBColor, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH
import fitz  # PyMuPDF

# ── Page config ──────────────────────────────────────────────────────────────
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
    """Render a single PDF page to a PNG and return base64."""
    doc = fitz.open(pdf_path)
    page = doc[page_index]
    mat = fitz.Matrix(2.0, 2.0)          # 2× zoom → ~144 dpi
    pix = page.get_pixmap(matrix=mat, alpha=False)
    png_bytes = pix.tobytes("png")
    doc.close()
    return base64.standard_b64encode(png_bytes).decode()


SYSTEM_PROMPT = """Eres un experto en OCR y transcripción de poesía.
Recibirás la imagen de una página de un libro de poemas.
Tu tarea es extraer el contenido estructurado de esa página.

Devuelve ÚNICAMENTE un JSON válido con esta forma (sin markdown, sin texto extra):

{
  "page_header": "texto del encabezado si existe, o null",
  "poems": [
    {
      "title": "TÍTULO DEL POEMA o null si no hay",
      "sections": [
        {
          "speaker": "Nombre del hablante o null si no hay",
          "lines": ["verso 1", "verso 2", ...]
        }
      ]
    }
  ],
  "footnotes": ["nota 1", "nota 2"]
}

Reglas:
- Puede haber 0, 1 o más poemas por página.
- Un poema puede tener múltiples secciones con distintos hablantes (ej. "El paciente:", "El médico:").
- Si no hay hablante, pon null en "speaker".
- Conserva la puntuación y acentos originales.
- Las notas al pie van en "footnotes" (pueden ser []).
- Si la página no tiene poemas (ej. es una página en blanco o solo encabezado), devuelve "poems": [].
"""


def extract_page(client: anthropic.Anthropic, b64_image: str, page_num: int) -> dict:
    """Call Claude to extract structured content from one page image."""
    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2000,
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
                        "text": f"Extrae el contenido estructurado de esta página (página {page_num}).",
                    },
                ],
            }
        ],
    )
    raw = message.content[0].text.strip()
    # Strip markdown fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw)


# ── Word document builder ─────────────────────────────────────────────────────

def build_docx(all_pages: list[dict]) -> bytes:
    doc = Document()

    # Default style
    style = doc.styles["Normal"]
    style.font.name = "Garamond"
    style.font.size = Pt(11)

    seen_titles = set()

    for page_data in all_pages:
        poems = page_data.get("poems", [])
        for poem in poems:
            title = poem.get("title")

            # Poem title
            if title and title not in seen_titles:
                seen_titles.add(title)
                p = doc.add_paragraph()
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                run = p.add_run(title)
                run.bold = True
                run.font.size = Pt(13)
                p.paragraph_format.space_before = Pt(18)
                p.paragraph_format.space_after = Pt(6)

            for section in poem.get("sections", []):
                speaker = section.get("speaker")
                if speaker:
                    sp = doc.add_paragraph()
                    sr = sp.add_run(speaker)
                    sr.italic = True
                    sr.font.size = Pt(11)
                    sp.paragraph_format.space_before = Pt(6)
                    sp.paragraph_format.space_after = Pt(2)

                for line in section.get("lines", []):
                    lp = doc.add_paragraph()
                    lp.add_run(line)
                    lp.paragraph_format.space_before = Pt(0)
                    lp.paragraph_format.space_after = Pt(1)
                    lp.paragraph_format.left_indent = Inches(0.4)

            # Small gap after each poem section block
            doc.add_paragraph().paragraph_format.space_after = Pt(6)

        # Footnotes
        footnotes = page_data.get("footnotes", [])
        for fn in footnotes:
            fp = doc.add_paragraph()
            fr = fp.add_run(fn)
            fr.font.size = Pt(9)
            fr.font.color.rgb = RGBColor(0x55, 0x55, 0x55)
            fp.paragraph_format.space_before = Pt(0)
            fp.paragraph_format.space_after = Pt(2)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".docx") as tmp:
        doc.save(tmp.name)
        tmp_path = tmp.name

    with open(tmp_path, "rb") as f:
        data = f.read()
    os.unlink(tmp_path)
    return data


# ── UI ────────────────────────────────────────────────────────────────────────

api_key = st.text_input(
    "🔑 Anthropic API Key",
    type="password",
    help="Tu clave de API de Anthropic. No se almacena.",
)

uploaded_file = st.file_uploader("📄 Sube tu PDF", type=["pdf"])

col1, col2 = st.columns([1, 2])
with col1:
    page_range = st.text_input(
        "Páginas a procesar (ej: 1-5, 10, 12-15)",
        value="todas",
        help='Deja "todas" para procesar todo el documento.',
    )

def parse_page_range(spec: str, total: int) -> list[int]:
    """Parse a page range string into a list of 0-based page indices."""
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


if st.button("🚀 Procesar PDF", disabled=not (api_key and uploaded_file)):
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
    all_pages: list[dict] = []
    errors = []

    for i, page_idx in enumerate(pages_to_process):
        status.text(f"Analizando página {page_idx + 1}…")
        try:
            b64 = pdf_page_to_base64(pdf_path, page_idx)
            result = extract_page(client, b64, page_idx + 1)
            result["_page"] = page_idx + 1
            all_pages.append(result)
        except Exception as e:
            errors.append(f"Página {page_idx + 1}: {e}")
        progress.progress((i + 1) / len(pages_to_process))

    os.unlink(pdf_path)
    progress.empty()
    status.empty()

    if errors:
        st.warning("Algunos errores:\n" + "\n".join(errors))

    if all_pages:
        st.success(f"✅ {len(all_pages)} páginas procesadas correctamente.")

        # Preview in expander
        with st.expander("🔍 Ver datos extraídos (JSON)"):
            st.json(all_pages)

        # Build Word doc
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
