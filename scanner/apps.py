from django.apps import AppConfig
from django.conf import settings
import logging

logger = logging.getLogger(__name__)


class ScannerConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'scanner'

    def ready(self):
        from scanner.services.ocr import configure_tesseract
        from scanner import signals  # noqa: F401

        command = getattr(settings, "TESSERACT_CMD", "tesseract")
        import os
        os.environ["TESSERACT_CMD"] = command
        if not configure_tesseract():
            logger.error(
                "OCR is unavailable. Set TESSERACT_CMD in .env to the installed Tesseract executable."
            )
