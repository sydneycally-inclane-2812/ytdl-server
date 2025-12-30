from celery import Celery

celery = Celery(
    "ytdl_worker",
    broker="redis://localhost:6379/0",
    backend="redis://localhost:6379/1",
)

celery.conf.task_routes = {
    "tasks.download_playlist": {"queue": "downloads"}
}
