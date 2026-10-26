from .models import SiteSettings


def site_settings(request):
    """Provides site branding settings (name, logo, favicon) to all templates."""
    try:
        return {"site_settings": SiteSettings.load()}
    except Exception:
        return {"site_settings": None}
