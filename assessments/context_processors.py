from .models import SiteSettings
from .permissions import is_team_member, role_label, user_permissions


def site_settings(request):
    """Provides site branding settings (name, logo, favicon) to all templates."""
    try:
        return {"site_settings": SiteSettings.load()}
    except Exception:
        return {"site_settings": None}


def team_access(request):
    """What the signed-in user may do, for showing or hiding links and buttons.

    ``oa_perms`` is a set of permission codes, used as ``{% if "tests.manage" in oa_perms %}``.
    Views still check permissions themselves; hiding a link is only for a cleaner page.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"oa_perms": frozenset(), "oa_is_team": False, "oa_role": ""}
    return {
        "oa_perms": user_permissions(user),
        "oa_is_team": is_team_member(user),
        "oa_role": role_label(user),
    }
