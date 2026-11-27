"""DaVinci Director — text-based editing for DaVinci Resolve."""

__version__ = "2.6.7"


class Cancelled(Exception):
    """The user cancelled a long job (download, install, transcription)."""
