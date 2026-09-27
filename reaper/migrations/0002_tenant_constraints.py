from django.db import migrations


CONSTRAINTS = [
    ("reaper_repositoryscan", "scan_repository_tenant_fk", "repository_id", "reaper_repository", "id"),
    ("reaper_repositoryanalysis", "analysis_scan_tenant_fk", "scan_id", "reaper_repositoryscan", "id"),
    ("reaper_resurrectionqueueitem", "queue_repository_tenant_fk", "repository_id", "reaper_repository", "id"),
]


def forwards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        for table, name, column, parent, target in CONSTRAINTS:
            schema_editor.execute(f'ALTER TABLE "{table}" ADD CONSTRAINT "{name}" FOREIGN KEY ("{column}", "user_id") REFERENCES "{parent}" ("{target}", "user_id") DEFERRABLE INITIALLY IMMEDIATE')


def backwards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        for table, name, *_ in CONSTRAINTS:
            schema_editor.execute(f'ALTER TABLE "{table}" DROP CONSTRAINT "{name}"')


class Migration(migrations.Migration):
    dependencies = [("reaper", "0001_initial")]
    operations = [migrations.RunPython(forwards, backwards)]
