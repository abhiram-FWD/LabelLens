"""Shared validation for browser and API image uploads."""

from django.core.exceptions import ValidationError
from PIL import Image, UnidentifiedImageError


MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGES_PER_SCAN = 6
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}


def validate_uploaded_images(images):
    images = list(images)
    errors = []
    if not images:
        raise ValidationError("Choose at least one image to upload.")
    if len(images) > MAX_IMAGES_PER_SCAN:
        errors.append(f"Upload no more than {MAX_IMAGES_PER_SCAN} images per scan.")

    for image in images:
        name = getattr(image, "name", "This file")
        if image.size > MAX_IMAGE_BYTES:
            errors.append(f"{name} is larger than 5 MB.")
            continue
        try:
            image.seek(0)
            with Image.open(image) as decoded:
                if decoded.format not in ALLOWED_FORMATS:
                    errors.append(f"{name} must be a JPEG, PNG, or WEBP image.")
                    continue
                decoded.verify()
        except (UnidentifiedImageError, OSError, ValueError):
            errors.append(f"{name} could not be read as an image. Please choose a clear JPEG, PNG, or WEBP photo.")
        finally:
            image.seek(0)

    if errors:
        raise ValidationError(errors)
