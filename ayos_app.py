"""Entry point for the packaged Windows program (Ayos.exe). From source, use:  python -m ayos"""
import sys

from ayos.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
