from datetime import timedelta

from django.contrib.sessions.models import Session
from django.core.management.base import BaseCommand
from django.utils import timezone

from reaper.models import GitHubCache, RateBucket


class Command(BaseCommand):
    help = "Prune expired sessions/caches and old operational rate buckets; run daily."

    def handle(self, *args, **options):
        GitHubCache.objects.filter(expires_at__lt=timezone.now() - timedelta(days=1)).delete()
        Session.objects.filter(expire_date__lt=timezone.now()).delete()
        RateBucket.objects.filter(updated_at__lt=timezone.now() - timedelta(days=2)).delete()
        self.stdout.write("Expired operational data removed.")
