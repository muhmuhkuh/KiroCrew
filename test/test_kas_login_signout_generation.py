"""A sign-out in another process fences an already-pending KAS login."""

from __future__ import annotations

import asyncio
import errno
import multiprocessing
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import kiro_crew.auth.store as store_module
import kiro_crew.platform_compat as platform_compat
from kiro_crew.auth.service import KasLoginService
from kiro_crew.auth.store import TokenStore, TokenStoreError
from kiro_crew.platform_compat import file_lock, open_lock_file
from kiro_crew.testing.wait import async_wait_until, await_until, default_timeout


class _Response:
    def __init__(self, payload):
        self.status = 200
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self, *, content_type="application/json"):
        return self.payload

    async def text(self):
        return str(self.payload)


class _Session:
    def __init__(self, payloads):
        self.responses = [_Response(payload) for payload in payloads]
        self.closed = False

    def post(self, *_args, **_kwargs):
        return self.responses.pop(0)

    async def close(self):
        self.closed = True


_AUTHORIZED = {
    "status": "authorized",
    "accessToken": "at-test",
    "refreshToken": "rt-test",
    "expiresIn": 3600,
    "profileArn": "arn:aws:kiro:us-east-1:1:profile/p1",
}


def _hold_signout_lock(directory: str, acquired, holder_inside, release) -> None:
    store = TokenStore(Path(directory))

    def _hold() -> None:
        holder_inside.set()
        acquired.set()
        try:
            release.wait()
        finally:
            holder_inside.clear()

    store.with_signout_lock(_hold)


def _guard_action(
    action,
    holder_inside,
    action_entered,
    action_while_held,
):
    """Return an action that fails if another process still holds the lock."""

    def _guarded_action():
        if holder_inside.is_set():
            action_while_held.set()
            raise AssertionError("sign-out action ran while another process held the lock")
        action_entered.set()
        return action()

    return _guarded_action


def _signout_lock_is_held(path: Path) -> bool:
    with open_lock_file(path, create=False) as fd:
        try:
            with file_lock(fd, wait=False):
                return False
        except BlockingIOError:
            return True


def _device_auth():
    from kiro_crew.auth.login import device

    return device.DeviceAuthorization(
        device_code="dc-test",
        user_code="WXYZ-1234",
        verification_uri="https://app.kiro.dev/device",
        verification_uri_complete="https://app.kiro.dev/device?user_code=WXYZ-1234",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=300),
        interval_secs=0.0,
    )


def _patch_device_start(monkeypatch):
    from kiro_crew.auth.login import device

    async def _initiate(_provider, *, session):
        return _device_auth()

    monkeypatch.setattr(device, "initiate_device_authorization", _initiate)


@pytest.mark.asyncio
async def test_signout_in_a_second_service_rejects_late_poll_then_allows_new_login(
    tmp_path, monkeypatch
):
    store = TokenStore(tmp_path)
    gateway = KasLoginService(store, session=_Session([_AUTHORIZED, _AUTHORIZED]))
    other_process = KasLoginService(TokenStore(tmp_path))
    _patch_device_start(monkeypatch)
    try:
        pending = await gateway.begin_device("google")
        await other_process.logout("social")
        late = await gateway.poll_device(pending["login_id"])
        assert late == {"status": "error", "code": "signed_out_during_login"}
        assert store.resolve() is None

        fresh = await gateway.begin_device("google")
        accepted = await gateway.poll_device(fresh["login_id"])
        assert accepted["status"] == "authorized"
        assert store.resolve() is not None
    finally:
        await gateway.close()
        await other_process.close()


@pytest.mark.asyncio
async def test_signout_lock_failure_is_reported(tmp_path, monkeypatch):
    store = TokenStore(tmp_path)
    service = KasLoginService(store)

    def _lock_failure(_action):
        raise TokenStoreError("could not take the sign-out lock")

    monkeypatch.setattr(store, "with_signout_lock", _lock_failure)
    with pytest.raises(TokenStoreError, match="sign-out lock"):
        await service.logout("social")
    await service.close()


@pytest.mark.asyncio
async def test_logout_waits_for_a_signout_lock_held_by_another_process(tmp_path, monkeypatch):
    context = multiprocessing.get_context("spawn")
    acquired = context.Event()
    holder_inside = context.Event()
    release = context.Event()
    holder = context.Process(
        target=_hold_signout_lock,
        args=(str(tmp_path), acquired, holder_inside, release),
    )
    store = TokenStore(tmp_path)
    service = KasLoginService(store)
    original_lock = store.with_signout_lock
    original_acquire = store_module.acquire_lock
    original_release = store_module.release_lock
    lock_path = store.signout_lock_path()
    lock_attempted = threading.Event()
    collision_observed = threading.Event()
    lock_acquired = threading.Event()
    lock_acquired_while_held = threading.Event()
    observed_acquire_fd = [None]
    action_entered = threading.Event()
    action_while_held = threading.Event()
    no_op_lock_attempted = threading.Event()
    no_op_continue = threading.Event()
    no_op_action_entered = threading.Event()
    no_op_action_while_held = threading.Event()
    no_op_logout = None
    logout = None
    holder_started = False

    def _no_op_acquire(_fd, *, exclusive=True):
        no_op_lock_attempted.set()
        if not no_op_continue.wait(default_timeout()):
            raise AssertionError("no-op lock continuation was not released")
        return None

    def _no_op_release(_fd):
        return None

    def _guard_with(action, action_entered_event, action_while_held_event):
        return original_lock(
            _guard_action(
                action,
                holder_inside,
                action_entered_event,
                action_while_held_event,
            )
        )

    def _guarded_no_op_lock(action):
        return _guard_with(action, no_op_action_entered, no_op_action_while_held)

    def _guarded_lock(action):
        return _guard_with(action, action_entered, action_while_held)

    def _observed_acquire(fd, *, exclusive=True):
        observed_acquire_fd[0] = fd
        lock_attempted.set()
        result = original_acquire(fd, exclusive=exclusive)
        if holder_inside.is_set():
            lock_acquired_while_held.set()
        lock_acquired.set()
        return result

    monkeypatch.setattr(store, "with_signout_lock", _guarded_no_op_lock)
    try:
        holder.start()
        holder_started = True
        await async_wait_until(
            lambda: acquired.is_set() or holder.exitcode is not None,
            off_loop=True,
            describe=lambda: f"holder exitcode={holder.exitcode}",
        )
        assert acquired.is_set(), "child exited without acquiring the sign-out lock"
        assert holder_inside.is_set()

        # Force the no-op service call to resume from its acquire point only after
        # the child owns the actual file lock. Keep that holder in place until the
        # service action itself proves the no-op would enter the critical section.
        monkeypatch.setattr(store_module, "acquire_lock", _no_op_acquire)
        monkeypatch.setattr(store_module, "release_lock", _no_op_release)
        no_op_logout = asyncio.create_task(service.logout("social"))
        await async_wait_until(
            lambda: no_op_lock_attempted.is_set() or no_op_logout.done(),
            describe=lambda: (
                f"no_op_lock_attempted={no_op_lock_attempted.is_set()}, "
                f"logout_done={no_op_logout.done()}"
            ),
        )
        assert no_op_lock_attempted.is_set(), "no-op service path did not reach acquire"
        no_op_continue.set()
        await async_wait_until(
            lambda: no_op_action_while_held.is_set() or no_op_logout.done(),
            describe=lambda: (
                f"action_while_held={no_op_action_while_held.is_set()}, "
                f"logout_done={no_op_logout.done()}"
            ),
        )
        assert no_op_action_while_held.is_set(), (
            "service action did not reject a no-op lock while another process held it"
        )
        assert not no_op_action_entered.is_set()
        try:
            await await_until(
                no_op_logout,
                what="no-op service path rejecting an unlocked critical section",
            )
        except AssertionError as exc:
            assert "another process held the lock" in str(exc)
        else:
            pytest.fail("the service path proceeded with a no-op process lock")

        monkeypatch.setattr(store_module, "acquire_lock", original_acquire)
        monkeypatch.setattr(store_module, "release_lock", original_release)
        monkeypatch.setattr(store, "with_signout_lock", _guarded_lock)
        action_while_held.clear()

        # Observe contention from the OS lock attempt made by original_acquire.
        # A Python-level signal before calling it would let cleanup release the
        # child between that signal and the actual lock operation.
        if platform_compat.IS_POSIX:
            original_flock = platform_compat.fcntl.flock

            def _observed_flock(fd, operation):
                try:
                    return original_flock(fd, operation)
                except BlockingIOError:
                    if (
                        fd == observed_acquire_fd[0]
                        and operation & platform_compat.fcntl.LOCK_NB
                        and holder_inside.is_set()
                    ):
                        collision_observed.set()
                    raise

            monkeypatch.setattr(platform_compat.fcntl, "flock", _observed_flock)
        else:
            original_msvcrt_locking = platform_compat.msvcrt.locking
            busy_errnos = (errno.EACCES, getattr(errno, "EDEADLOCK", errno.EDEADLK))

            def _observed_msvcrt_locking(fd, mode, count):
                try:
                    return original_msvcrt_locking(fd, mode, count)
                except OSError as exc:
                    if (
                        fd == observed_acquire_fd[0]
                        and mode == platform_compat.msvcrt.LK_NBLCK
                        and count == 1
                        and exc.errno in busy_errnos
                        and holder_inside.is_set()
                    ):
                        collision_observed.set()
                    raise

            monkeypatch.setattr(platform_compat.msvcrt, "locking", _observed_msvcrt_locking)

        monkeypatch.setattr(store_module, "acquire_lock", _observed_acquire)
        logout = asyncio.create_task(service.logout("social"))
        await async_wait_until(
            lambda: collision_observed.is_set() or logout.done(),
            describe=lambda: (
                f"lock_attempted={lock_attempted.is_set()}, "
                f"collision_observed={collision_observed.is_set()}, "
                f"logout_done={logout.done()}"
            ),
        )
        assert lock_attempted.is_set(), "logout completed without reaching the process lock"
        assert collision_observed.is_set(), (
            "service acquire did not observe the child's held file lock before release"
        )
        assert holder_inside.is_set()
    finally:
        release.set()
        no_op_continue.set()
        try:
            if no_op_logout is not None and not no_op_logout.done():
                try:
                    await await_until(
                        no_op_logout,
                        what="no-op service path after test-controlled continuation",
                    )
                except AssertionError:
                    pass
            if logout is not None:
                await await_until(logout, what="logout after the process lock is released")
        finally:
            try:
                await service.close()
            finally:
                if holder_started:
                    await asyncio.to_thread(holder.join, default_timeout())
                    if holder.is_alive():
                        holder.terminate()
                        await asyncio.to_thread(holder.join, default_timeout())
                    if holder.is_alive():
                        holder.kill()
                        await asyncio.to_thread(holder.join, default_timeout())
                    assert not holder.is_alive(), (
                        "lock-holder process survived bounded terminate and kill joins"
                    )
    assert holder.exitcode == 0
    assert lock_acquired.is_set()
    assert not lock_acquired_while_held.is_set(), (
        "service acquire returned while another process still held the lock"
    )
    assert action_entered.is_set()
    assert not action_while_held.is_set()
