"""
Sends an invoice image to Google Cloud Vision API and returns the extracted text.

PDFs are converted to images page-by-page using pdf2image (requires poppler-utils).
Images (JPEG, PNG, etc.) are sent directly.
"""
import os
import tempfile
from google.cloud import vision
from google.oauth2 import service_account
from config import CREDENTIALS_PATH


def get_vision_client():
    credentials = service_account.Credentials.from_service_account_file(
        CREDENTIALS_PATH,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    return vision.ImageAnnotatorClient(credentials=credentials)


def _ocr_image_bytes(client, content: bytes) -> str:
    """Send raw image bytes to Vision API. Returns extracted text."""
    image = vision.Image(content=content)
    response = client.document_text_detection(image=image)
    if response.error.message:
        raise RuntimeError(f"Vision API error: {response.error.message}")
    return response.full_text_annotation.text


def extract_text(image_path: str) -> str:
    """
    Run OCR on a local file (image or PDF). Returns the full raw text detected.
    For PDFs, each page is OCR'd and the results are concatenated.

    Results are cached locally by file content hash to avoid repeat API charges.
    """
    import ocr_cache
    cached = ocr_cache.get(image_path, "vision_ocr")
    if cached is not None:
        print(f"   [Vision] Cache hit — skipping API call")
        return cached

    client = get_vision_client()

    MAX_PDF_PAGES = 30  # safety limit to prevent OOM on huge PDFs

    if image_path.lower().endswith(".pdf"):
        from pdf2image import convert_from_path, pdfinfo_from_path
        info = pdfinfo_from_path(image_path)
        total_pages = info.get("Pages", 0)
        if total_pages > MAX_PDF_PAGES:
            print(f"  [!] PDF has {total_pages} pages, limiting OCR to first {MAX_PDF_PAGES}")
        pages = convert_from_path(image_path, dpi=200,
                                  last_page=min(total_pages, MAX_PDF_PAGES) if total_pages else None)
        texts = []
        for page in pages:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                page.save(tmp.name, "PNG")
                tmp_path = tmp.name
            try:
                with open(tmp_path, "rb") as f:
                    content = f.read()
                texts.append(_ocr_image_bytes(client, content))
            finally:
                os.remove(tmp_path)
        result = "\n".join(texts)
        ocr_cache.put(image_path, "vision_ocr", result)
        return result

    with open(image_path, "rb") as f:
        content = f.read()
    result = _ocr_image_bytes(client, content)
    ocr_cache.put(image_path, "vision_ocr", result)
    return result
