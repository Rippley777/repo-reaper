from django.core.management.base import BaseCommand
from django.db import transaction

from reaper.models import AIAccount, GitHubAccount, GitHubCache
from reaper.services.security import decrypt, encrypt


class Command(BaseCommand):
    help = "Re-encrypt credentials/caches using the first TOKEN_ENCRYPTION_KEYS key."

    def handle(self, *args, **options):
        for account_id in AIAccount.objects.values_list("pk", flat=True).iterator():
            with transaction.atomic():
                account = AIAccount.objects.select_for_update().filter(pk=account_id).first()
                if account:
                    account.api_key = encrypt(decrypt(account.api_key))
                    account.save(update_fields=["api_key"])
        for account_id in GitHubAccount.objects.values_list("pk", flat=True).iterator():
            with transaction.atomic():
                account = GitHubAccount.objects.select_for_update().filter(pk=account_id).first()
                if account:
                    account.access_token = encrypt(decrypt(account.access_token))
                    account.refresh_token = encrypt(decrypt(account.refresh_token))
                    account.save(update_fields=["access_token", "refresh_token"])
        for cache_id in GitHubCache.objects.values_list("pk", flat=True).iterator():
            with transaction.atomic():
                cache = GitHubCache.objects.select_for_update().filter(pk=cache_id).first()
                if cache:
                    cache.body = encrypt(decrypt(cache.body))
                    cache.save(update_fields=["body"])
        self.stdout.write("Credentials and cache encryption rotated successfully.")
