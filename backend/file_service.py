"""
File upload processing — PDF/DOCX text extraction, and image understanding
via Groq's vision model (qwen/qwen3.8-27b). Uses the raw Groq SDK for the
vision call since langchain-groq's message conversion can silently drop
multimodal (image_url) content blocks depending on the installed version.
"""
import base64
import io
import os
from typing import Tuple

from groq import Groq

GROQ_API_KEY = (os.getenv("GROQ_API_KEY") or "").strip()
VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "qwen/qwen3.8-27b")

MAX_FILE_BYTES = 15 * 1024 * 1024   # stay comfortably under Groq's 20MB image limit
MAX_EXTRACTED_CHARS = 12000          # cap injected doc text so it doesn't dominate context

_groq_client = None


def _get_groq_client() -> Groq:
    global _groq_client
    if _groq_client is None:
        _groq_client = Groq(api_key=GROQ_API_KEY)
    return _groq_client


def extract_pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    text = "\n\n".join((page.extract_text() or "") for page in reader.pages).strip()
    if not text:
        raise ValueError("No extractable text found — this PDF may be scanned/image-only.")
    return text[:MAX_EXTRACTED_CHARS]


def extract_docx_text(data: bytes) -> str:
    from docx import Document
    doc = Document(io.BytesIO(data))
    text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    if not text.strip():
        raise ValueError("No extractable text found in this document.")
    return text[:MAX_EXTRACTED_CHARS]


def describe_image(data: bytes, mime_type: str, question: str = "") -> str:
    b64 = base64.b64encode(data).decode("utf-8")
    prompt = question.strip() or "Describe this image in detail — objects, text, context, anything notable."
    response = _get_groq_client().chat.completions.create(
        model=VISION_MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime_type or 'image/jpeg'};base64,{b64}"}},
            ],
        }],
        max_tokens=900,
        temperature=0,
        reasoning_effort="none",
        reasoning_format="hidden",
    )
    return (response.choices[0].message.content or "").strip()


_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def process_upload(filename: str, mime_type: str, data: bytes, question: str = "") -> Tuple[str, str]:
    """Returns (kind, extracted_text_or_description). kind is 'pdf' | 'docx' | 'image'."""
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("File too large — please upload something under 15MB.")

    name_lower = (filename or "").lower()
    mime_type = mime_type or ""

    if mime_type.startswith("image/") or name_lower.endswith(_IMAGE_EXTS):
        return "image", describe_image(data, mime_type, question)
    if name_lower.endswith(".pdf") or mime_type == "application/pdf":
        return "pdf", extract_pdf_text(data)
    if name_lower.endswith(".docx") or "wordprocessingml" in mime_type:
        return "docx", extract_docx_text(data)

    raise ValueError("Unsupported file type — please upload a PDF, DOCX, or image (jpg/png/webp).")