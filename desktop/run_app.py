"""Entry point for the packaged app (PyInstaller) and for running from source: python run_app.py"""
import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from reelscutter.__main__ import main
    sys.exit(main())
