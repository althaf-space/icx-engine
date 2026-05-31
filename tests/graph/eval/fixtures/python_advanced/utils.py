import importlib


def load_plugin(plugin_name: str):
    module = importlib.import_module(f"myapp.{plugin_name}")
    return module


def load_adapter(adapter_path: str):
    mod = importlib.import_module(adapter_path)
    return mod
