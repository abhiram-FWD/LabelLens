"""Storage cleanup for file-backed scan records deleted through any cascade."""

import logging

from django.db.models.signals import post_delete
from django.dispatch import receiver

from scanner.models import Report, ScanImage

logger = logging.getLogger(__name__)


def _delete_stored_file(field_file, *, owner):
    name = field_file.name
    if not name:
        return
    try:
        field_file.storage.delete(name)
    except Exception:
        logger.exception("Could not delete stored file %s for %s", name, owner)


@receiver(post_delete, sender=ScanImage)
def delete_scan_image_file(sender, instance, **kwargs):
    _delete_stored_file(instance.image, owner=f"scan image {instance.pk}")


@receiver(post_delete, sender=Report)
def delete_report_file(sender, instance, **kwargs):
    _delete_stored_file(instance.pdf_file, owner=f"report for scan {instance.scan_id}")
