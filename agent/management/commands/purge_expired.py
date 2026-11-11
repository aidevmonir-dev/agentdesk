from django.core.management.base import BaseCommand

from agent.views import purge_expired


class Command(BaseCommand):
    help = "Delete demo workspaces older than RETENTION_HOURS. Safe to run from cron."

    def handle(self, *args, **options):
        self.stdout.write(f"Deleted {purge_expired()} rows from expired workspaces.")
