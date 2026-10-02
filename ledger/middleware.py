from .api import health_api

# Mirrors the route in litebooks/urls.py. Both spellings are matched so a probe
# that omits the trailing slash is answered instead of being redirected.
HEALTH_PATHS = ("/healthz/", "/healthz")


class HealthCheckMiddleware:
    """Answer the liveness probe before Django validates the Host header.

    Probes reach the container by service name or container IP -- the updater
    sidecar uses http://web:8000/healthz/ -- and operators never list those in
    ALLOWED_HOSTS, so host validation turns the one request that must always
    answer into a 400. That silently breaks every in-app update, because the
    updater reads `sha` from here to confirm the new build is really serving.

    This must stay first in MIDDLEWARE: SecurityMiddleware and CommonMiddleware
    both call request.get_host(), which is what raises DisallowedHost.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path in HEALTH_PATHS:
            return health_api(request)
        return self.get_response(request)
