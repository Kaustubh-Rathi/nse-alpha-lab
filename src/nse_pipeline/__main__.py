"""``python -m nse_pipeline`` entry point.

Kept as a two-line shim so the console script (``nse-alpha``) and the module
form share exactly one implementation and one exit-code contract.
"""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
