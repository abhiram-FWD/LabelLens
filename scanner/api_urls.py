from django.urls import path

from .api import ScanCreateAPIView

urlpatterns = [
    path("scan/", ScanCreateAPIView.as_view(), name="scan-create"),
]
