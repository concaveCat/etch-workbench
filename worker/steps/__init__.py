"""Per-model executors for the worker's ``execute`` step loop.

Each module owns one branch of the loop that used to be inline in
``worker/simulate.py``. The shared loop state lives in ``RunContext`` so the
executors stay free of module-level cycles: native helpers are resolved from
``worker.simulate`` at call time.
"""
