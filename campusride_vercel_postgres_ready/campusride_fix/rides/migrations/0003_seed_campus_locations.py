from django.db import migrations


CAMPUS_LOCATIONS = [
    ("Main Gate", 7.4436, 3.8970),
    ("Library", 7.4450, 3.8990),
    ("Hostel Area", 7.4410, 3.8950),
    ("Faculty Complex", 7.4460, 3.9010),
    ("Sports Complex", 7.4400, 3.8930),
]


def seed_locations(apps, schema_editor):
    CampusLocation = apps.get_model("rides", "CampusLocation")
    for name, latitude, longitude in CAMPUS_LOCATIONS:
        CampusLocation.objects.get_or_create(
            name=name,
            defaults={"latitude": latitude, "longitude": longitude},
        )


def unseed_locations(apps, schema_editor):
    CampusLocation = apps.get_model("rides", "CampusLocation")
    names = [name for name, _, _ in CAMPUS_LOCATIONS]
    CampusLocation.objects.filter(name__in=names).delete()


class Migration(migrations.Migration):
    dependencies = [("rides", "0002_or_mvp_plan_fields")]
    operations = [migrations.RunPython(seed_locations, unseed_locations)]
