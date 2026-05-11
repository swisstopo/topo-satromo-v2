"""
_terrain_worker.py  --  Subprocess shim for create_terrain_mask.

Called by util_reprocess_terrain_tap.py as an isolated subprocess so that
HORAYZON/Embree scene state is never shared across iterations.

Usage (internal -- do not call directly):
    python _terrain_worker.py dev_config.py \\
        --orbit 22 --timedate 2025-01-07t101319 --output output.tif

The first positional argument (config file) is consumed here to satisfy
configuration/__init__.py, which reads sys.argv[1] as the config filename.
All remaining arguments are forwarded to create_terrain_mask via argparse.

Why if __name__ == '__main__':
    On Windows, multiprocessing (spawn) re-imports this file in every worker
    process.  During that re-import __name__ is '__mp_main__', not '__main__',
    so the guarded block is skipped and the workers never try to start a second
    Pool -- which would trigger the "bootstrapping phase" RuntimeError.
"""

import sys
import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Minimal module-level setup: only path manipulation so that worker processes
# that re-import this file can still find main_functions.main_terrain_parallel.
# Everything else lives under if __name__ == '__main__'.
# ---------------------------------------------------------------------------
THIS_DIR = Path(__file__).resolve().parent
ROOT     = THIS_DIR.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(THIS_DIR))


if __name__ == '__main__':
    import logging

    # -----------------------------------------------------------------------
    # Stage sys.argv so configuration/__init__.py sees a valid config filename
    # at position 1.  Save the full argv to restore for our own argparse later.
    # -----------------------------------------------------------------------
    _full_argv = sys.argv[:]

    if len(_full_argv) > 1 and not _full_argv[1].startswith('-'):
        _config_file = _full_argv[1]
        _rest_argv   = _full_argv[2:]
    else:
        _config_file = 'dev_config.py'
        _rest_argv   = _full_argv[1:]

    sys.argv = [_full_argv[0], _config_file]

    import configuration  # noqa: E402  # primes sys.modules before main_terrain_parallel loads it

    sys.argv = [_full_argv[0]] + _rest_argv

    # -----------------------------------------------------------------------
    # Initialise logging early so any import-time errors are visible.
    # create_terrain_mask will reinitialise logging internally -- that is fine.
    # -----------------------------------------------------------------------
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        stream=sys.stdout,
        force=True,
    )

    # -----------------------------------------------------------------------
    # Import create_terrain_mask.
    # main_terrain_parallel uses a relative import (from .main_terrain_module)
    # so it must be imported as part of the main_functions package.
    # -----------------------------------------------------------------------
    from main_functions.main_terrain_parallel import create_terrain_mask  # noqa: E402

    # -----------------------------------------------------------------------
    # Parse arguments.
    # Defaults allow direct VS Code execution without a launch.json.
    # -----------------------------------------------------------------------
    from argparse import ArgumentParser

    parser = ArgumentParser(description='Terrain mask worker shim')
    parser.add_argument('--orbit',    default=None, help="Sentinel-2 orbit ID or 'CH'")
    parser.add_argument('--timedate', default=None, help='UTC datetime YYYY-MM-DDtHHMMSS')
    parser.add_argument('--output',   default=None, help='Output GeoTIFF path')
    args = parser.parse_args()

    if args.orbit is None:
        args.orbit    = '22'
        args.timedate = '2026-03-03t101019'
        args.output   = None
        logging.info('No arguments provided -- using VS Code debug defaults')

    logging.info(
        f'_terrain_worker: orbit={args.orbit}, timedate={args.timedate}, '
        f'output={args.output}, cwd={os.getcwd()}'
    )

    # -----------------------------------------------------------------------
    # Run
    # Reset sys.argv to [script, config_file] before calling create_terrain_mask.
    # When the internal multiprocessing Pool (spawn) forks worker processes on
    # Windows it copies the parent's sys.argv into each worker.  If the full
    # argv is present, configuration/__init__.py in the worker sees sys.argv[2]
    # as the orbit flag and tries to parse it as a date, then calls sys.exit(1).
    # Restoring argv to the two-element safe form avoids this.
    # -----------------------------------------------------------------------
    sys.argv = [_full_argv[0], _config_file]

    try:
        success = create_terrain_mask(
            orbit=args.orbit,
            timedate=args.timedate,
            outputfilename=args.output,
        )
    except Exception as exc:
        logging.exception(f'create_terrain_mask raised an unhandled exception: {exc}')
        success = False

    if not success:
        logging.error(
            'create_terrain_mask returned False -- check the log output above '
            'for the root cause (look for [ERROR] or [CRITICAL] lines).'
        )

    sys.exit(0 if success else 1)
