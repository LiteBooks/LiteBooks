from collections import defaultdict

from django.db import migrations, models


def renumber_entries(apps, schema_editor):
    JournalEntry = apps.get_model("ledger", "JournalEntry")
    EntryNumberSequence = apps.get_model("ledger", "EntryNumberSequence")
    entries = list(JournalEntry.objects.order_by("date", "created_at", "pk"))
    for entry in entries:
        entry.number = f"TMP-{entry.pk}"
        entry.save(update_fields=["number"])
    counters = defaultdict(int)
    for entry in entries:
        counters[entry.date] += 1
        entry.number = f"{entry.date:%Y%m%d}-{counters[entry.date]:04d}"
        entry.save(update_fields=["number"])
    for entry_date, last_value in counters.items():
        EntryNumberSequence.objects.update_or_create(entry_date=entry_date, defaults={"last_value": last_value})


class Migration(migrations.Migration):
    dependencies = [("ledger", "0002_entrynumbersequence")]

    operations = [
        migrations.DeleteModel(name="EntryNumberSequence"),
        migrations.CreateModel(
            name="EntryNumberSequence",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("entry_date", models.DateField(unique=True)),
                ("last_value", models.PositiveIntegerField(default=0)),
            ],
        ),
        migrations.RunPython(renumber_entries, migrations.RunPython.noop),
    ]
