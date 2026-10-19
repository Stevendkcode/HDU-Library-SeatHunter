"""Booking runner: execute bookings with retry logic.

Extracted from main.py:157-172 (startNow) and killer.py:416-422 (run).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from time import sleep
from typing import List, Callable, Optional

from seathunter.api.client import ApiClient
from seathunter.auth.session_manager import SessionManager
from seathunter.models.plan import Plan
from seathunter.models.booking_result import BookingResult

logger = logging.getLogger("seathunter.scheduler")

# Phase 2 (extended retry) polls this many seconds apart, to catch seats
# released after another user's abandoned/timeout hold.
EXTENDED_POLL_INTERVAL = 45


class BookingRunner:
    """Executes booking attempts with retry logic."""

    def __init__(self, api_client: ApiClient, session_manager: SessionManager,
                 interval: int = 5, max_try_times: int = 10,
                 extended_retry_minutes: int = 0):
        self.api = api_client
        self.session_mgr = session_manager
        self.interval = interval
        self.max_try_times = max_try_times
        self.extended_retry_minutes = extended_retry_minutes
        self._cancelled = False

    def cancel(self):
        """Cancel the current booking run."""
        self._cancelled = True

    def prewarm(self):
        """Warm the TCP/TLS connection to the booking server. Never raises
        (ApiClient.prewarm_connection swallows all errors)."""
        self.api.prewarm_connection()

    def _interruptible_wait(self, seconds: float):
        """Sleep in <=1s chunks so cancel() takes effect within ~1 second."""
        deadline = time.monotonic() + max(0.0, seconds)
        while not self._cancelled and time.monotonic() < deadline:
            sleep(min(1.0, deadline - time.monotonic()))

    def run_booking(self, plans: List[Plan], target_date: datetime,
                    on_result: Optional[Callable[[BookingResult], None]] = None,
                    on_attempt: Optional[Callable[[int, int], None]] = None) -> List[BookingResult]:
        """Execute booking for all plans targeting a specific date.

        Two phases:
        - Phase 1: up to ``max_try_times`` dense attempts, ``interval`` seconds apart.
        - Phase 2 (if ``extended_retry_minutes`` > 0): poll every
          ``EXTENDED_POLL_INTERVAL`` seconds until the deadline, to catch
          seats freed by other users' abandoned holds.

        Args:
            plans: List of Plan objects to book.
            target_date: The date the seats are for.
            on_result: Callback for each booking result.
            on_attempt: Callback for each attempt (attempt_num, plan_index).

        Returns:
            List of BookingResult for all attempts.
        """
        self._cancelled = False
        results = []

        # Freeze settings at run start: the GUI can mutate the attributes
        # mid-run, which used to produce inconsistent logs (e.g. "attempt 9/5")
        max_try = self.max_try_times
        extended_min = self.extended_retry_minutes

        # Phase 1: dense retries
        for retry in range(max_try):
            if self._cancelled:
                break

            if on_attempt:
                on_attempt(retry + 1, -1)
            logger.info("Booking attempt %d/%d for %s",
                       retry + 1, max_try,
                       target_date.strftime("%Y-%m-%d"))

            if self._attempt_all_plans(plans, target_date, results, on_result):
                return results

            if retry < max_try - 1:
                logger.debug("Waiting %ds before retry...", self.interval)
                self._interruptible_wait(self.interval)

        # Phase 2: extended polling to catch seats freed by other users'
        # abandoned holds.
        if not self._cancelled and extended_min > 0:
            deadline = time.monotonic() + extended_min * 60
            logger.info("阶段1共%d次尝试全部失败，进入延长重试阶段（每%d秒一轮，持续%d分钟）",
                       max_try, EXTENDED_POLL_INTERVAL, extended_min)
            while not self._cancelled:
                # Sleep until the next poll, capped at the deadline
                next_poll = min(time.monotonic() + EXTENDED_POLL_INTERVAL, deadline)
                self._interruptible_wait(next_poll - time.monotonic())
                if self._cancelled or time.monotonic() >= deadline:
                    break
                logger.info("延长重试轮询（剩余约%.1f分钟）for %s",
                           (deadline - time.monotonic()) / 60,
                           target_date.strftime("%Y-%m-%d"))
                if self._attempt_all_plans(plans, target_date, results, on_result):
                    return results

        # Single cancellation/exit log point
        if self._cancelled:
            logger.info("Booking run cancelled")
        elif extended_min > 0:
            logger.info("延长重试阶段结束")
        return results

    def _attempt_all_plans(self, plans: List[Plan], target_date: datetime,
                           results: List[BookingResult],
                           on_result: Optional[Callable[[BookingResult], None]] = None) -> bool:
        """Try every plan once, in order. Returns True if any plan succeeded."""
        for plan in plans:
            if self._cancelled:
                break

            # Build the actual datetime for the plan
            hour, minute, second = (int(x) for x in plan.begin_time.split(":"))
            begin_time = target_date.replace(hour=hour, minute=minute, second=second, microsecond=0)

            # Get seat IDs and booker UIDs
            seat_ids = [s.seat_id for s in plan.seats]
            booker_uids = [self.session_mgr.uid] * len(plan.seats)

            result = self._book_single_plan(plan, begin_time, seat_ids, booker_uids, target_date)
            results.append(result)

            if on_result:
                on_result(result)

            if result.success:
                logger.info("Booking successful: %s - %s", plan.id, plan.room_name)
                return True

            logger.warning("Plan %s failed: %s", plan.id, result.message)

            if self._cancelled:
                break

        return False

    def _book_single_plan(self, plan: Plan, begin_time: datetime,
                          seat_ids: List[str], booker_uids: List[str],
                          target_date: datetime) -> BookingResult:
        """Execute a single booking attempt for one plan."""
        try:
            resp = self.api.book_seat(begin_time, plan.duration_hours, seat_ids, booker_uids)
            return BookingResult.from_api_response(
                resp,
                plan_id=plan.id,
                seat_num=",".join(s.seat_num for s in plan.seats),
                room_name=plan.room_name,
                target_date=target_date.strftime("%Y-%m-%d"),
            )
        except Exception as e:
            logger.error("Booking exception for plan %s: %s", plan.id, e)
            return BookingResult(
                success=False,
                code="error",
                message=str(e),
                plan_id=plan.id,
                target_date=target_date.strftime("%Y-%m-%d"),
            )
