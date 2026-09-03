from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied

from .models import UserProfile


ROLE_LEVEL = {
    UserProfile.Role.VIEWER: 10,
    UserProfile.Role.BOOKKEEPER: 20,
    UserProfile.Role.ADMIN: 30,
    UserProfile.Role.OWNER: 40,
}


def user_role(user):
    if user.is_superuser:
        return UserProfile.Role.OWNER
    profile, _ = UserProfile.objects.get_or_create(user=user)
    return profile.role


def role_required(minimum_role):
    def decorator(view):
        @login_required
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if ROLE_LEVEL[user_role(request.user)] < ROLE_LEVEL[minimum_role]:
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return wrapped

    return decorator


def can_edit_books(user):
    return ROLE_LEVEL[user_role(user)] >= ROLE_LEVEL[UserProfile.Role.BOOKKEEPER]


def can_administer(user):
    return ROLE_LEVEL[user_role(user)] >= ROLE_LEVEL[UserProfile.Role.ADMIN]

