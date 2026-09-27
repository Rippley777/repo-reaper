import signal
import time

from django.core.management.base import BaseCommand
from django.db import OperationalError, close_old_connections

from reaper.services.jobs import claim_job, run_job


class Command(BaseCommand):
    help = "Run durable repository scan jobs. Use PostgreSQL for multiple workers."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        self.running = True
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        while self.running:
            close_old_connections()
            try:
                scan = claim_job()
                if scan:
                    run_job(scan)
            except OperationalError:
                self.stderr.write("Database unavailable; retrying shortly.")
                scan = None
            if options["once"]:
                break
            if not scan:
                time.sleep(2)

    def stop(self, *args):
        self.running = False
