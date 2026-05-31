from myapp.services import create_user_and_notify, run_report


def test_create_user(db_session, test_client):
    result = create_user_and_notify({"name": "Alice", "email": "alice@example.com"})
    assert result["id"] == 1


def test_run_report(db_session):
    run_report("monthly")
