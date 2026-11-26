from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from scanner.views import health

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("scanner.api_urls")),
    path("health/", health, name="health"),
    path("accounts/", include("accounts.urls")),
    path("", include("scanner.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
