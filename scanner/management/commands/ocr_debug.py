"""Print each OCR attempt and save its source variant for inspection."""

import os
import tempfile

import cv2
from django.core.management.base import BaseCommand, CommandError

from scanner.services.ocr import iter_ocr_attempts


class Command(BaseCommand):
    help = "Diagnose OCR variants for an image and save preprocessed variants to a temp folder."

    def add_arguments(self, parser):
        parser.add_argument("image_path")

    def handle(self, *args, **options):
        image_path = options["image_path"]
        if not os.path.isfile(image_path):
            raise CommandError(f"Image does not exist: {image_path}")
        output_dir = tempfile.mkdtemp(prefix="ocr-debug-")
        try:
            for attempt in iter_ocr_attempts(image_path):
                label = f"{attempt['variant']}_psm{attempt['psm']}"
                if "image" in attempt:
                    image_path = os.path.join(output_dir, f"{attempt['variant']}.png")
                    if not os.path.exists(image_path):
                        cv2.imwrite(image_path, attempt["image"])
                self.stdout.write(
                    f"{label}: confidence={attempt['confidence']:.2f}, words={attempt['word_count']}\n"
                    f"{attempt.get('text', '')}\n"
                )
            self.stdout.write(self.style.SUCCESS(f"Preprocessed images saved to {output_dir}"))
        except Exception as exc:
            raise CommandError(f"OCR debug failed: {exc}") from exc
