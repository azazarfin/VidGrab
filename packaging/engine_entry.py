"""Service and owned-worker modes share the frozen executable and support tree."""

if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    import sys
    if sys.argv[1:2] == ["--worker-owner"]:
        from backend.engine.worker_owner import main
        main(sys.argv[2:])
    elif sys.argv[1:2] == ["--worker"]:
        from backend.engine.worker import main
        main()
    else:
        from backend.engine.__main__ import main
        main()
