import streamlit as st
import json
import tempfile
import os
import requests
import time

from docx import Document
from docx.shared import Pt, RGBColor, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
import fitz  # PyMuPDF

# ── CONFIG ───────────────────────────────────────────────────────────────────

HF_API_URL = "https://api-inference.huggingface.co/models/mistralai/Mistral-7B-Instruct-v0.2"

st.set_page_config(
    page_title="Poetry OCR Extractor",
    page_icon="📖",
    layout="wide",
)

st.title("📖 Poetry OCR Extractor (Hugging Face)")
st.markdown(
    "Sube un PDF con poemas. Extrae el texto y usa **Hugging Face (gratis)** "
    "para limpiar y estructurar el contenido."
)

# ── PDF HELPERS ──────────────────────────────────────────────────────────────

def extract_text_half(pdf_path: str, page_index: int, half: str) -> str:
    doc = fitz.open(pdf_path)
    page = doc[page_index]
    rect = page.rect

    if half == "left":
        clip = fitz.Rect(rect.x0, rect.y0, rect.x1 / 2, rect.y1)
    elif half == "right":
        clip = fitz.Rect(rect.x1 / 2, rect.y0, rect.x1, rect.y1)
    else:
        clip = rect

    text = page.get_text("text", clip=clip, sort=True)
    doc.close()
    return text.strip()


def is_text_empty(text: str) -> bool:
    return len(text.replace("\n", "").replace(" ", "")) < 20


def detect_page_mode(pdf_path: str, page_index: int) -> str:
    doc = fitz.open(pdf_path)
    page = doc[page_index]
    ratio = page.rect.width / page.rect.height
    doc.close()
    return "double" if ratio > 1.2 else "single"


# ── PROMPT ───────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Eres un experto en edición y transcripción de poesía hispanohablante.
Devuelve ÚNICAMENTE JSON válido con esta estructura:

{
  "blank": false,
  "page_header": null,
  "poems": [],
  "footnotes": []
}

Reglas:
- Elimina números de línea, encabezados, anotaciones (GB¹, etc.)
- Mantén versos intactos
- Detecta títulos en mayúsculas
- Separa hablantes si existen
- Si no hay contenido: blank=true
"""


# ── HUGGING FACE CALL ─────────────────────────────────────────────────────────

def structure_page_hf(api_key: str, raw_text: str, label: str) -> dict:
    headers = {"Authorization": f"Bearer {api_key}"}

    prompt = f"""{SYSTEM_PROMPT}

Texto crudo de {label}:

{raw_text[:4000]}

Devuelve solo JSON.
"""

    payload = {
        "inputs": prompt,
        "parameters": {
            "temperature": 0.1,
            "max_new_tokens": 1200,
            "return_full_text": False,
        },
    }

    for attempt in range(3):
        response = requests.post(HF_API_URL, headers=headers, json=payload)

        if response.status_code == 503:
            time.sleep(5)
            continue

        if response.status_code != 200:
            raise Exception(response.text)

        result = response.json()
        generated_text = result[0]["generated_text"]

        # limpiar markdown
        if "```" in generated_text:
            parts = generated_text.split("```")
            generated_text = parts[1] if len(parts) > 1 else generated_text
            if generated_text.startswith("json"):
                generated_text = generated_text[4:]

        return json.loads(generated_text.strip())

    raise Exception("HF API no respondió tras varios intentos")


# ── DOCX BUILDER ─────────────────────────────────────────────────────────────

def add_page_break(doc: Document):
    p = doc.add_paragraph()
    run = p.add_run()
    br = OxmlElement("w:br")
    br.set(qn("w:type"), "page")
    run._r.append(br)


def build_docx(all_pages):
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Garamond"
    style.font.size = Pt(11)

    for page in all_pages:
        for poem in page.get("poems", []):
            title = poem.get("title")
            if title:
                p = doc.add_paragraph()
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                run = p.add_run(title)
                run.bold = True
                run.font.size = Pt(13)

            for section in poem.get("sections", []):
                speaker = section.get("speaker")
                if speaker:
                    sp = doc.add_paragraph()
                    sp.add_run(speaker).italic = True

                for line in section.get("lines", []):
                    lp = doc.add_paragraph()
                    lp.add_run(line)
                    lp.paragraph_format.left_indent = Inches(0.5)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".docx") as tmp:
        doc.save(tmp.name)
        path = tmp.name

    with open(path, "rb") as f:
        data = f.read()

    os.unlink(path)
    return data


# ── UI ───────────────────────────────────────────────────────────────────────

api_key = st.text_input(
    "🔑 Hugging Face API Key",
    type="password",
    placeholder="hf_...",
)

uploaded_file = st.file_uploader("📄 Sube tu PDF", type=["pdf"])

if st.button("🚀 Procesar PDF", disabled=not (api_key and uploaded_file)):

    if not api_key.startswith("hf_"):
        st.error("API key inválida (debe empezar con hf_)")
        st.stop()

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(uploaded_file.read())
        pdf_path = tmp.name

    pdf = fitz.open(pdf_path)
    total_pages = len(pdf)
    pdf.close()

    progress = st.progress(0)
    all_pages = []

    for i in range(total_pages):
        raw = extract_text_half(pdf_path, i, "full")

        try:
            if is_text_empty(raw):
                all_pages.append({"blank": True, "poems": []})
            else:
                result = structure_page_hf(api_key, raw, f"página {i+1}")
                all_pages.append(result)

        except Exception as e:
            st.error(f"Error en página {i+1}: {e}")

        progress.progress((i + 1) / total_pages)

    os.unlink(pdf_path)

    st.success("Procesamiento completo")

    with st.spinner("Generando Word..."):
        docx = build_docx(all_pages)

    st.download_button(
        "⬇️ Descargar Word",
        data=docx,
        file_name="poemas.docx",
    )
