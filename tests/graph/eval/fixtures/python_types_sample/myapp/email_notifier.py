from myapp.protocols import Notifier

class EmailNotifier:
    def send(self, message: str) -> bool:
        print(f"email: {message}")
        return True
