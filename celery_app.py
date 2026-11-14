"""Celery app: Redis broker and result store, and the beat schedule.

All times are India time. The schedule only fires from 09:00 to 20:59; the tasks also check the
clock themselves (tasks.py), so a task that sat in a queue past 21:00 does nothing.

    celery -A celery_app worker --concurrency 2 --loglevel info
    celery -A celery_app beat --loglevel info
"""
from celery import Celery
from celery.schedules import crontab

import config

app = Celery("mas", broker=config.REDIS_URL, backend=config.REDIS_URL, include=["tasks"])
app.conf.update(
    timezone=config.TIMEZONE,
    enable_utc=False,                      # crontab hours below are India time
    task_acks_late=True,                   # a crashed worker's task goes back to the queue
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,          # long LLM calls: do not hoard tasks
    broker_connection_retry_on_startup=True,
    result_expires=3600,
    task_default_expires=config.SCAN_EVERY_MINUTES * 60,
)

# hour="9-20" means 09:00 to 20:59. Nothing is scheduled at 21:00 or later, or before 09:00.
WINDOW_HOURS = "9-20"

app.conf.beat_schedule = {
    "scan-and-enqueue": {
        "task": "scan_and_enqueue",
        "schedule": crontab(minute=f"*/{config.SCAN_EVERY_MINUTES}", hour=WINDOW_HOURS),
        "options": {"expires": config.SCAN_EVERY_MINUTES * 60 - 60},
    },
    "release-held-drafts": {
        "task": "release_held_drafts",
        "schedule": crontab(minute="*/5", hour=WINDOW_HOURS),
        "options": {"expires": 240},
    },
}
