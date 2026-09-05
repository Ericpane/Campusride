from django.contrib import admin
from .models import Profile, CampusLocation, ActiveTrip, RideRequest, TripHistory

admin.site.register(Profile)
admin.site.register(CampusLocation)
admin.site.register(ActiveTrip)
admin.site.register(RideRequest)
admin.site.register(TripHistory)
