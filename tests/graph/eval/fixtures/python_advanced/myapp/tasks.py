from celery import shared_task


@shared_task
def send_email(user_id: int, subject: str) -> bool:
    return True


@shared_task
def generate_report(report_type: str) -> dict:
    return {"status": "done"}


@shared_task
def cleanup_old_data(days: int = 30) -> int:
    return 0
