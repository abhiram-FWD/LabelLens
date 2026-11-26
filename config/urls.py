import re

from django.conf import settings
from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.urls import include, path, re_path
from django.views.static import serve

from scanner.views import health

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("scanner.api_urls")),
    path("health/", health, name="health"),
    path("accounts/", include("accounts.urls")),
    path("", include("scanner.urls")),
    re_path(
        rf"^{re.escape(settings.MEDIA_URL.lstrip('/'))}(?P<path>.*)$",
        login_required(serve),
        {"document_root": settings.MEDIA_ROOT},
        name="protected_media",
    ),
]
