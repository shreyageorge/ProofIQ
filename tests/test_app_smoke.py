"""Smoke tests for the active Django presentation layer."""
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dataguard.settings")

import django

django.setup()

from django.test import Client


def test_dashboard_renders_without_login_or_external_auth():
    response = Client().get("/", HTTP_HOST="localhost")
    assert response.status_code == 200
    assert b"ProofIQ" in response.content
    assert b"Ask your data" in response.content
    assert b"Sign in with OpenAI" not in response.content
    assert b"Sign in with ChatGPT" not in response.content


def test_upload_form_is_csrf_protected():
    response = Client(enforce_csrf_checks=True).post("/upload/", {}, HTTP_HOST="localhost")
    assert response.status_code == 403
