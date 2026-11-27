"""DaVinci Director watcher — started hidden at Windows login (HKCU Run, set by install.py).

DaVinci Resolve's script sandbox cannot start programs, so this small process waits
in the background and opens the DaVinci Director window as soon as you run
Workspace > Scripts > DaVinci Director in Resolve.  No window, no tray icon.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from davinci_director.__main__ import main  # noqa: E402

sys.exit(main(["--background"] + sys.argv[1:]))
