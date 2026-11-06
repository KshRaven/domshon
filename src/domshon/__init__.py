__version__ = "0.1.0"
APP_NAME = "Domshon"

_LAZY = ("Remote", "Watcher", "connect", "RemoteError", "ApiError", "SyncResult")
__all__ = ["__version__", "APP_NAME", *_LAZY]


def __getattr__(name):  # keep `import domshon` light; load the client library on first use
    if name in _LAZY:
        from . import remote

        return getattr(remote, name)
    raise AttributeError(f"module 'domshon' has no attribute {name!r}")
