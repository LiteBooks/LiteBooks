from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from ledger import api, spa


urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz/", api.health_api, name="healthz"),
    path("api/", include("ledger.api_urls")),
    path("accounts/login/", spa.legacy_login),
    path("", include("ledger.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
