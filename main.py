"""
HDU Library SeatHunter - Main Entry Point

Usage:
    python main.py                  # GUI mode (default)
    python main.py --cli            # CLI mode (terminal interface)
    python main.py --daemon         # Daemon mode (no menu, reads config and runs)
    python main.py -c path/to/config.yaml  # Custom config path
"""

import os
import sys
import signal
import logging
import time
import warnings

from argparse import ArgumentParser

# Python version check
_MIN_PYTHON = (3, 8)
if sys.version_info < _MIN_PYTHON:
    sys.exit(
        f"SeatHunter requires Python >= {_MIN_PYTHON[0]}.{_MIN_PYTHON[1]}, "
        f"current: {sys.version_info.major}.{sys.version_info.minor}"
    )

# Suppress typing module DeprecationWarning on Python 3.12+
if sys.version_info >= (3, 12):
    warnings.filterwarnings("ignore", category=DeprecationWarning, module="typing")


def get_app_dir():
    """Get application root directory (PyInstaller-compatible)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def setup_path():
    """Add project root to Python path for imports."""
    app_dir = get_app_dir()
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)


def parse_args():
    """Parse command-line arguments."""
    parser = ArgumentParser(description="HDU Library SeatHunter")
    parser.add_argument(
        "-c", "--config",
        type=str,
        default=os.path.join(get_app_dir(), "config", "config.yaml"),
        help="Config file path (default: config/config.yaml)",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run in daemon mode (no interactive menu, reads config and starts scheduler)",
    )
    parser.add_argument(
        "--cli",
        action="store_true",
        help="Use CLI mode instead of GUI",
    )
    return parser.parse_args()


def run_interactive(config_path: str):
    """Run in interactive mode with GUI (default) or CLI."""
    try:
        import tkinter as tk
    except ImportError:
        run_cli(config_path)
        return

    from seathunter.logging_.logger import setup_logging
    from seathunter.config.manager import ConfigManager
    from seathunter.auth.session_manager import SessionManager
    from seathunter.api.client import ApiClient
    from seathunter.api.room_cache import RoomCache
    from seathunter.ui.gui import GuiApp

    logger = setup_logging()
    logger.info("SeatHunter starting (GUI mode)")

    # Initialize components
    config = ConfigManager(config_path)
    config.load()

    session_mgr = SessionManager(config)
    session_mgr.init_session()

    api_client = ApiClient(session_mgr)
    room_cache = RoomCache(api_client)

    # Create and run GUI
    try:
        root = tk.Tk()
    except tk.TclError:
        print("无法初始化图形界面，切换到命令行模式")
        run_cli(config_path)
        return

    # Hide console after GUI window is ready
    from seathunter.platform_.window import hide_console
    hide_console()

    app = GuiApp(root, config, session_mgr, api_client, room_cache)
    root.mainloop()


def run_cli(config_path: str):
    """Run in CLI mode with full terminal menu."""
    from seathunter.logging_.logger import setup_logging
    from seathunter.config.manager import ConfigManager
    from seathunter.auth.session_manager import SessionManager
    from seathunter.api.client import ApiClient
    from seathunter.api.room_cache import RoomCache
    from seathunter.ui.cli import CliUI
    from seathunter.platform_.window import maximize_window

    # Setup
    maximize_window()
    logger = setup_logging()
    logger.info("SeatHunter starting (CLI mode)")

    # Initialize components
    config = ConfigManager(config_path)
    config.load()

    session_mgr = SessionManager(config)
    session_mgr.init_session()

    api_client = ApiClient(session_mgr)
    room_cache = RoomCache(api_client)

    # Create and run UI
    ui = CliUI(config, session_mgr, api_client, room_cache)
    ui.login()
    ui.run()


def run_daemon(config_path: str):
    """Run in daemon mode: read config, start scheduler, no menu."""
    from seathunter.logging_.logger import setup_logging
    from seathunter.config.manager import ConfigManager
    from seathunter.auth.session_manager import SessionManager
    from seathunter.api.client import ApiClient
    from seathunter.api.room_cache import RoomCache
    from seathunter.scheduler.engine import SchedulerEngine
    from seathunter.scheduler.booking_runner import BookingRunner
    from seathunter.logging_.history import HistoryLogger

    logger = setup_logging()
    logger.info("SeatHunter starting (daemon mode)")

    # Initialize components
    config = ConfigManager(config_path)
    config.load()

    schedules = config.get_schedules()
    active_schedules = [s for s in schedules if s.enabled]
    plans = config.get_plans()

    if not active_schedules:
        logger.error("No active schedules found in config. Exiting.")
        sys.exit(1)
    if not plans:
        logger.error("No plans found in config. Exiting.")
        sys.exit(1)

    logger.info("Found %d active schedule(s) and %d plan(s)",
               len(active_schedules), len(plans))

    # Login
    session_mgr = SessionManager(config)
    session_mgr.init_session()

    success, err = session_mgr.login()
    if not success:
        logger.error("Login failed: %s", err)
        sys.exit(1)
    logger.info("Login successful: uid=%s", session_mgr.uid)

    # Setup API and room cache
    api_client = ApiClient(session_mgr)
    room_cache = RoomCache(api_client)

    # Background room data refresh
    room_cache.start_background_refresh()

    # Settings
    settings = config.get_settings()
    runner = BookingRunner(
        api_client=api_client,
        session_manager=session_mgr,
        interval=settings["interval"],
        max_try_times=settings["max_try_times"],
        extended_retry_minutes=settings.get("extended_retry_minutes", 0),
    )

    engine = SchedulerEngine(
        config_manager=config,
        session_manager=session_mgr,
        booking_runner=runner,
    )

    history = HistoryLogger()

    # Engine callbacks (log-only in daemon mode)
    def on_countdown(remaining, trigger_time, plan_desc):
        from seathunter.ui.display import format_countdown
        logger.info(
            "Countdown: %s -> %s | Remaining: %s",
            trigger_time.strftime("%Y-%m-%d %H:%M"),
            plan_desc,
            format_countdown(remaining),
        )

    def on_result(result):
        history.log(result)
        if result.success:
            logger.info("Booking result: %s", result)
        else:
            logger.warning("Booking result: %s", result)

    def on_start(target_date, plan_ids):
        logger.info("Booking starting for %s, plans: %s",
                    target_date.strftime("%Y-%m-%d"), ", ".join(plan_ids))

    def on_error(error):
        logger.error("Engine error: %s", error)

    engine.on_countdown_tick = on_countdown
    engine.on_booking_result = on_result
    engine.on_booking_start = on_start
    engine.on_error = on_error

    # Handle SIGTERM/SIGINT for clean shutdown
    def signal_handler(signum, frame):
        logger.info("Received signal %s, shutting down...", signum)
        engine.stop()
        room_cache.stop_background_refresh()
        sys.exit(0)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Start engine
    engine.start()
    logger.info("Scheduler engine started in daemon mode")

    # Block main thread while engine runs
    try:
        while engine.is_running:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt, shutting down...")
        engine.stop()
        room_cache.stop_background_refresh()


def main():
    """Main entry point."""
    setup_path()
    args = parse_args()

    if args.daemon:
        run_daemon(args.config)
    elif args.cli:
        run_cli(args.config)
    else:
        run_interactive(args.config)


if __name__ == "__main__":
    main()
