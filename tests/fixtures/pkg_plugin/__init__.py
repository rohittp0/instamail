"""Package-form fixture: re-exports its plugin class (tests the package discovery path)."""

from .plugin import PkgPlugin

__all__ = ["PkgPlugin"]
