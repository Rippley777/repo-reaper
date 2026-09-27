from django.urls import path
from reaper import views

urlpatterns = [
    path("", views.landing, name="landing"),
    path("login", views.login, name="login"),
    path("logout", views.logout, name="logout"),
    path("auth/github", views.oauth_start, name="oauth_start"),
    path("auth/github/callback", views.oauth_callback, name="oauth_callback"),
    path("dashboard", views.dashboard, name="dashboard"),
    path("repositories", views.repositories, name="repositories"),
    path("repositories/import", views.import_repositories, name="import_repositories"),
    path("repos/<uuid:repo_id>", views.repo_detail, name="repo_detail"),
    path("repos/<uuid:repo_id>/rescan", views.rescan, name="rescan"),
    path("repos/<uuid:repo_id>/delete", views.delete_repository, name="delete_repository"),
    path("repos/<uuid:repo_id>/queue", views.queue_update, name="queue_update"),
    path("api/scans/<uuid:scan_id>", views.scan_status, name="scan_status"),
    path("graveyard", views.graveyard, name="graveyard"),
    path("queue", views.queue, name="queue"),
    path("settings", views.account_settings, name="settings"),
    path("settings/save", views.save_settings, name="save_settings"),
    path("settings/ai-key", views.save_ai_key, name="save_ai_key"),
    path("settings/ai-key/delete", views.delete_ai_key, name="delete_ai_key"),
    path("settings/privacy", views.privacy_action, name="privacy_action"),
    path("health", views.health, name="health"),
]
