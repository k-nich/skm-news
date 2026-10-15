"""Create or refresh the Admin, Journalist, and Reader groups.

Idempotent: safe to run as often as you like. It sets each group's
permissions to exactly the list defined below, so editing ROLE_PERMISSIONS
and re-running keeps the groups in sync.

Run manually:   python manage.py setup_roles
It also runs automatically on every Railway deploy (see the Procfile).
"""
from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

# Permissions are written as "app_label.codename". Django auto-creates
# add/change/delete/view permissions for every model.
ROLE_PERMISSIONS = {
    "Admin": [
        # Full control of content...
        "news.add_section", "news.change_section", "news.delete_section", "news.view_section",
        "news.add_story", "news.change_story", "news.delete_story", "news.view_story",
        "news.edit_any_story", "news.lock_story",
        # ...and the ability to manage other accounts.
        "auth.add_user", "auth.change_user", "auth.delete_user", "auth.view_user",
        "auth.add_group", "auth.change_group", "auth.delete_group", "auth.view_group",
    ],
    "Editor": [
        # Edit ANY story and lock/unlock it; can't manage users.
        "news.add_story", "news.change_story", "news.view_story",
        "news.edit_any_story", "news.lock_story",
        "news.view_section",
    ],
    "Journalist": [
        # Write and edit only their OWN stories (enforced in the admin);
        # see sections but don't restructure them. No lock, no edit-any.
        "news.add_story", "news.change_story", "news.view_story",
        "news.view_section",
    ],
    # Readers need no admin permissions — the site is publicly readable.
    # The group is a forward-looking label for future reader-only features.
    "Reader": [],
}


class Command(BaseCommand):
    help = "Create/refresh the Admin, Journalist, and Reader groups and their permissions."

    def handle(self, *args, **options):
        for group_name, perm_specs in ROLE_PERMISSIONS.items():
            group, created = Group.objects.get_or_create(name=group_name)

            perms, missing = [], []
            for spec in perm_specs:
                app_label, codename = spec.split(".")
                try:
                    perms.append(
                        Permission.objects.get(
                            content_type__app_label=app_label, codename=codename
                        )
                    )
                except Permission.DoesNotExist:
                    missing.append(spec)

            group.permissions.set(perms)

            verb = "Created" if created else "Updated"
            self.stdout.write(
                self.style.SUCCESS(f"{verb} '{group_name}' with {len(perms)} permission(s).")
            )
            if missing:
                self.stdout.write(
                    self.style.WARNING(
                        f"  Skipped missing permissions (run migrations first?): {', '.join(missing)}"
                    )
                )

        self.stdout.write(self.style.SUCCESS("Roles are in sync."))
        self.stdout.write(
            "Reminder: to let someone use /admin/, assign their group AND tick "
            "'Staff status' on their user. Readers should NOT be staff."
        )
