"""Instagram enrichment plugin: best-effort email->username resolution + anonymous
public-profile harvesting via the web_profile_info endpoint.

The plugin class is re-exported here so the loader can resolve it: when load_plugins
imports this package, it registers the BasePlugin subclass exposed in this namespace.
"""

from .plugin import InstagramPlugin

__all__ = ["InstagramPlugin"]
