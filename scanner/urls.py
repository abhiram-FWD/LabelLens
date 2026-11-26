from django.urls import path

from . import views

app_name = "scanner"
urlpatterns = [
    path("", views.home, name="home"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("upload/", views.upload, name="upload"),
    path("history/", views.history, name="history"),
    path("scans/<int:scan_id>/", views.result, name="result"),
    path("scans/<int:scan_id>/report/", views.download_report, name="download_report"),
    path("scans/<int:scan_id>/rescan/", views.rescan, name="rescan"),
    path("scans/<int:scan_id>/delete/", views.delete_scan, name="delete_scan"),
]
