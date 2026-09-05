from django.core.management.base import BaseCommand
from rides.models import ActiveTrip
from rides.planning import plan_trip


class Command(BaseCommand):
    help = "Run revenue-max planning for all active trips with pending requests"

    def handle(self, *args, **options):
        trips = ActiveTrip.objects.filter(
            status__in=["active", "forming", "full"], seats_available__gt=0
        )
        for trip in trips:
            if trip.requests.filter(status="pending").exists():
                self.stdout.write(f"Planning trip {trip.id}...")
                accepted = plan_trip(trip)
                self.stdout.write(f"  Accepted {len(accepted)} riders.")
        self.stdout.write(self.style.SUCCESS("Done"))
