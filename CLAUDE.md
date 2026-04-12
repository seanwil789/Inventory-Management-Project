# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Django 5.2 SaaS application. The project uses SQLite for local development and is configured for PostgreSQL in production via `dj-database-url`. Static files are served by WhiteNoise and the app is deployed with Gunicorn.

## Commands

```bash
# Activate virtualenv
source .venv/bin/activate

# Run dev server
python manage.py runserver

# Run all tests
python manage.py test

# Run tests for a specific app
python manage.py test myapp

# Run a single test
python manage.py test myapp.tests.MyTestClass.test_method

# Database migrations
python manage.py makemigrations
python manage.py migrate

# Create superuser
python manage.py createsuperuser
```

## Architecture

- `myproject/` — Django project package: settings, root URL conf, WSGI/ASGI entrypoints.
- `myapp/` — Primary application (models, views, admin, tests). Currently a skeleton; all feature work goes here.
- `myproject/settings.py` — Uses SQLite locally; wire in `DATABASE_URL` env var via `dj-database-url` for production PostgreSQL.
- Static files: `STATIC_ROOT` should be set to `staticfiles/` (already gitignored) and collected with `manage.py collectstatic` for production.

## Environment

Create a `.env` file (gitignored) for local overrides. At minimum, production needs:
- `SECRET_KEY`
- `DATABASE_URL`
- `ALLOWED_HOSTS`
- `DEBUG=False`
