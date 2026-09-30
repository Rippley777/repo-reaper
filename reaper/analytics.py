from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator


def browser_analytics():
    """Only public browser configuration reaches HTML or the CSP."""
    endpoint = settings.HOUSE_EDGE_ENDPOINT.strip()
    key = settings.HOUSE_EDGE_KEY.strip()
    if not settings.HOUSE_EDGE_ENABLED or not key or not endpoint:
        return None
    try:
        URLValidator(schemes=["http", "https"])(endpoint)
        url = urlsplit(endpoint)
        if url.username is not None or url.password is not None:
            return None
    except (ValidationError, ValueError):
        return None
    return {
        "project": settings.HOUSE_EDGE_PROJECT,
        "key": key,
        "endpoint": endpoint,
        "origin": f"{url.scheme}://{url.netloc.lower()}",
    }
