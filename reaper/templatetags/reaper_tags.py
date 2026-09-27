from django import template

register = template.Library()


@register.simple_tag(takes_context=True)
def querystring(context, **kwargs):
    query = context["request"].GET.copy()
    query.update(kwargs)
    return query.urlencode()


@register.filter
def status_class(value):
    return {
        "Production / Shipped": "green",
        "Nearly Complete": "blue",
        "Functional Prototype": "purple",
        "Early Prototype": "amber",
        "Abandoned": "muted",
        "Archive / Reference": "muted",
        "High": "green",
        "Medium": "amber",
        "Low": "muted",
        "Tiny": "green",
        "Small": "green",
        "Large": "amber",
    }.get(value, "muted")
