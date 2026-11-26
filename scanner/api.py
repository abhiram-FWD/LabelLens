from rest_framework import status
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from django.core.exceptions import ValidationError
import logging

from scanner.models import Scan, ScanImage
from scanner.services.pipeline import process_scan
from scanner.services.upload_validation import validate_uploaded_images

logger = logging.getLogger(__name__)


class ScanCreateAPIView(APIView):
    authentication_classes = (SessionAuthentication, TokenAuthentication)
    permission_classes = (IsAuthenticated,)
    parser_classes = (MultiPartParser, FormParser)

    def post(self, request):
        images = request.FILES.getlist("images")
        if not images:
            return Response({"detail": "Upload at least one image using the 'images' field."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            validate_uploaded_images(images)
        except ValidationError as exc:
            return Response({"detail": exc.messages}, status=status.HTTP_400_BAD_REQUEST)

        scan = Scan.objects.create(user=request.user)
        for image in images:
            ScanImage.objects.create(scan=scan, image=image)
        processing_error = None
        try:
            process_scan(scan)
        except Exception:
            logger.exception("Scan processing failed for API scan %s", scan.pk)
            scan.status = Scan.Status.NEEDS_REVIEW
            scan.score = None
            scan.ocr_confidence = 0
            scan.save(update_fields=("status", "score", "ocr_confidence"))
            processing_error = "We could not read these photos. Please upload clearer images or verify the details manually."

        product = scan.product
        return Response({
            "scan_id": scan.pk,
            "status": scan.status,
            "score": scan.score,
            "ocr_confidence": scan.ocr_confidence,
            "extracted_text": scan.extracted_text,
            "detail": processing_error,
            "product": ({"id": product.pk, "name": product.name, "brand": product.brand, "barcode": product.barcode} if product else None),
            "fields": {
                item.field_name: {"value": item.value, "confidence": item.confidence}
                for item in scan.extracted_fields.all()
            },
            "rule_results": [{
                "code": result.rule.code,
                "name": result.rule.name,
                "status": result.status,
                "found_value": result.found_value,
                "evidence": result.evidence,
                "source_image_side": result.source_image.get_side_display() if result.source_image_id else None,
                "legal_reference": result.legal_reference,
                "message": result.message,
                "suggested_fix": result.suggested_fix,
            } for result in scan.rule_results.select_related("rule", "source_image").all()],
        }, status=status.HTTP_201_CREATED)
