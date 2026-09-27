from django.conf import settings


def site(request):
    return {
        "github_configured": bool(settings.GITHUB_CLIENT_ID and settings.GITHUB_CLIENT_SECRET),
        "github_install_url": f"https://github.com/apps/{settings.GITHUB_APP_SLUG}/installations/new"
        if settings.GITHUB_APP_SLUG
        else "",
        "models": settings.AI_MODELS,
    }
