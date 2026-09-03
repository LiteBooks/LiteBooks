from django.shortcuts import redirect, render
from django.views.decorators.csrf import ensure_csrf_cookie


@ensure_csrf_cookie
def spa(request, **kwargs):
    return render(request, "spa.html")


def legacy_login(request):
    return redirect("login")
