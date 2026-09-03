from .permissions import can_administer, can_edit_books, user_role


def navigation_context(request):
    if not request.user.is_authenticated:
        return {}
    return {
        "current_role": user_role(request.user),
        "can_edit_books": can_edit_books(request.user),
        "can_administer": can_administer(request.user),
    }
