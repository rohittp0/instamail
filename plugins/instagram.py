"""Drop-in registration for the Instagram plugin.

The loader only registers BasePlugin subclasses *defined* in the loaded module, so this
file subclasses the real implementation (which lives in the installed instamail.instagram
package) to make it discoverable. All logic is in instamail.instagram.plugin.
"""

from instamail.instagram.plugin import InstagramPlugin as _InstagramPlugin


class InstagramPlugin(_InstagramPlugin):
    pass
