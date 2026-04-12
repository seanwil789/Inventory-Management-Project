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
    """
    client = get_vision_client()

    if image_path.lower().endswith(".pdf"):
        from pdf2image import convert_from_path
        pages = convert_from_path(image_path, dpi=200)
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
        return "\n".join(texts)

    with open(image_path, "rb") as f:
        content = f.read()
    return _ocr_image_bytes(client, content)
