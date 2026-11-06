from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.http import Http404
from django.urls import include, path, re_path

from ledger import api, mcp, spa


urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz/", api.health_api, name="healthz"),
    path("api/", include("ledger.api_urls")),
    # Both spellings, so a client configured without the slash is not redirected mid-POST.
    path("mcp/", mcp.mcp_endpoint, name="mcp"),
    path("mcp", mcp.mcp_endpoint),
    path("accounts/login/", spa.legacy_login),
    path("", include("ledger.urls")),
]



def private_media(request, path):
    raise Http404()


if settings.DEBUG:
    # Founder receipts are private: they download only through the authenticated
    # /api/founder-expenses/receipts/<id>/download/ endpoint, even in development.
    urlpatterns += [re_path(rf"^{settings.MEDIA_URL.strip('/')}/attachments/founder/(?P<path>.*)$", private_media)]
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
