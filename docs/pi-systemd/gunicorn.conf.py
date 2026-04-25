# Gunicorn config for production-shaped deployment on the Pi.
# Pre-staged — not active until you swap django.service ExecStart to:
#   /home/sean/my-saas/.venv/bin/gunicorn -c docs/pi-systemd/gunicorn.conf.py myproject.wsgi:application
#
# Reasoning behind the values:
# - 2 workers fits the Pi 4 4-core, 8GB profile. With sqlite, more workers
#   contend on the DB lock — 2 is the sweet spot for read-heavy traffic
#   from a wall display + occasional writes from cron.
# - sync worker class is fine for this workload (no async I/O,
#   no long-polling). gthread / async would be premature.
# - timeout=120 covers the slowest legitimate request (COGs page when
#   the OCR cache rebuild is happening).
# - max_requests=1000 with jitter recycles workers periodically to
#   avoid memory growth from any leaks in OCR libs.

bind = "0.0.0.0:8000"
workers = 2
worker_class = "sync"
timeout = 120
graceful_timeout = 30
keepalive = 5

# Worker recycling
max_requests = 1000
max_requests_jitter = 100

# Logging — append to the same log file the runserver service uses
# so log inspection is uniform.
accesslog = "/home/sean/my-saas/logs/gunicorn-access.log"
errorlog = "/home/sean/my-saas/logs/gunicorn-error.log"
loglevel = "info"

# Access log format — minimal, no IP (everything is tailnet anyway)
access_log_format = '%(t)s %(s)s %(L)ss "%(m)s %(U)s%(q)s"'

# Don't write a pidfile — systemd manages process lifecycle.
proc_name = "my-saas-gunicorn"
