"""PyInstaller entrypoint; multiprocessing workers remain a P03 interface."""

if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    from backend.engine.__main__ import main
    main()
