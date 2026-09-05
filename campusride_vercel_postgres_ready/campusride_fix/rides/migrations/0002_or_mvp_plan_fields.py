from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("rides", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="activetrip",
            name="expected_revenue",
            field=models.FloatField(default=0),
        ),
        migrations.AddField(
            model_name="activetrip",
            name="planned_route_json",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="activetrip",
            name="plan_explanation",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AlterField(
            model_name="activetrip",
            name="destination",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="trips_as_anchor",
                to="rides.campuslocation",
            ),
        ),
        migrations.AlterField(
            model_name="activetrip",
            name="status",
            field=models.CharField(
                choices=[
                    ("forming", "Forming"),
                    ("active", "Active"),
                    ("full", "Full"),
                    ("completed", "Completed"),
                ],
                default="forming",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="riderequest",
            name="detour_meters",
            field=models.FloatField(default=0),
        ),
        migrations.AddField(
            model_name="riderequest",
            name="p_board",
            field=models.FloatField(default=0.5),
        ),
    ]
