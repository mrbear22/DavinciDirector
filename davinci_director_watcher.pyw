"""DaVinci Director, started hidden at Windows login (HKCU Run).

It opens its window when Workspace > Scripts > DaVinci Director runs in Resolve.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from davinci_director.__main__ import main  # noqa: E402

sys.exit(main(sys.argv[1:]))
