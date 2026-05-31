from myapp.tasks import send_email, generate_report


def create_user_and_notify(user_data: dict) -> dict:
    user = {"id": 1, **user_data}
    send_email.delay(user["id"], "Welcome!")
    return user


def run_report(report_type: str) -> None:
    generate_report.apply_async(args=[report_type], countdown=10)


def bulk_notify(user_ids: list) -> None:
    for uid in user_ids:
        send_email.delay(uid, "Notification")
