"""Entry point for the packaged desktop executable (no console window on Windows).

Started without arguments it opens the app; the boot entry passes `tray --minimized`.
"""
import sys

from domshon.cli import main

if __name__ == "__main__":
    main(sys.argv[1:] or ["gui"])
