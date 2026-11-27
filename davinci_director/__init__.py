"""DaVinci Director — text-based editing for DaVinci Resolve."""

__version__ = "2.6.7"
PORT = 47615  # local server of the panel; not 8765, which ReCut uses


class Cancelled(Exception):
    """The user cancelled a long job (download, install, transcription)."""
