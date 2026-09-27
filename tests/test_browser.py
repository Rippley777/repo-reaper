from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from reaper.models import Repository, RepositoryAnalysis, RepositoryScan
from django.utils import timezone

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


def test_desktop_mobile_navigation_queue_and_theme(client, users, result, live_server):
    # pytest-django's server doesn't serve static assets unless its handler is overridden.
    repo = Repository.objects.create(
        user=users[0],
        github_id=9001,
        owner="alice",
        name="signal-worker",
        description="A durable task queue exploring retries, concurrency, and distributed workers.",
        language="Python",
        pushed_at=timezone.now(),
    )
    scan = RepositoryScan.objects.create(
        user=users[0],
        repository=repo,
        status="complete",
        available_at=timezone.now(),
        completed_at=timezone.now(),
        analysis_version="1.0",
        model="gpt-4.1-mini",
        evidence={"limitations": ["Sampled evidence"]},
    )
    RepositoryAnalysis.objects.create(
        scan=scan,
        user=users[0],
        result=result.model_dump(),
        project_status=result.project_status,
        resurrection_effort=result.resurrection_effort,
        portfolio_value=result.portfolio_value,
    )
    client.force_login(users[0])
    session = client.cookies["sessionid"].value
    artifacts = Path("artifacts")
    artifacts.mkdir(exist_ok=True)
    errors = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 1040}, color_scheme="dark")
        page = context.new_page()
        page.set_default_timeout(7000)
        page.on("pageerror", lambda error: errors.append(str(error)))
        # Serve local assets through routing so the test doesn't depend on collectstatic.
        page.route(
            "**/static/**",
            lambda route: route.fulfill(
                path=Path("static") / route.request.url.split("/static/")[1]
            ),
        )
        page.goto(live_server.url)
        assert page.get_by_role("heading", name="Good code deserves a second life.").is_visible()
        page.screenshot(path=str(artifacts / "landing-desktop.png"), full_page=True)
        context.add_cookies([{"name": "sessionid", "value": session, "url": live_server.url}])
        page.goto(live_server.url + "/dashboard")
        assert page.get_by_role("heading", name="Your project landscape.").is_visible()
        page.get_by_role("button", name="Expand audit").click()
        assert page.get_by_role("heading", name="What Was This?").is_visible()
        page.get_by_role("button", name="Collapse audit").click()
        page.screenshot(path=str(artifacts / "dashboard-desktop.png"), full_page=True)
        page.get_by_role("link", name="signal-worker", exact=True).click()
        assert page.get_by_role("heading", name="Project audit", exact=True).is_visible()
        page.get_by_role("button", name="Add to queue").click()
        assert page.get_by_role("heading", name="Resurrection Queue.").is_visible()
        page.get_by_label("Priority", exact=True).select_option("Next")
        page.get_by_label("Notes", exact=True).fill("Test the worker recovery path first.")
        page.get_by_role("button", name="Save", exact=True).click()
        assert (
            page.get_by_label("Notes", exact=True).input_value()
            == "Test the worker recovery path first."
        )
        page.get_by_role("button", name="Toggle light and dark mode").click()
        assert page.locator("html").get_attribute("data-theme") == "light"
        page.screenshot(path=str(artifacts / "queue-light.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(live_server.url + "/dashboard")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.screenshot(path=str(artifacts / "dashboard-mobile.png"), full_page=True)
        page.goto(live_server.url + "/settings")
        assert page.get_by_role("heading", name="Settings & privacy.").is_visible()
        assert page.get_by_role("button", name="Sign out", exact=True).is_visible()
        page.get_by_role("button", name="Sign out", exact=True).click()
        assert page.get_by_role("link", name="Sign in", exact=True).is_visible()
        assert not errors
        browser.close()
