"""Structured work under one revocable vault task."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from sys import exc_info
from types import TracebackType
from typing import TYPE_CHECKING, Any, TypeVar

from .errors import VaultProtocolError
from .scope import KeyScope, VaultClosed, VaultError

if TYPE_CHECKING:
    import asyncpg

    from .service import LeaseService, LeaseTicket

T = TypeVar("T")
ExceptionState = tuple[type[BaseException] | None, BaseException | None, TracebackType | None]


class LeaseWork:
    def __init__(self, service: LeaseService, scope: KeyScope, ticket: LeaseTicket) -> None:
        self.scope = scope
        # Transport and subprocess shutdown may unblock a cancelling child.
        self.resources = AsyncExitStack()
        self.finalizers = AsyncExitStack()
        self._service = service
        self._initial = ticket
        self._progress = asyncio.Event()
        self._children = asyncio.TaskGroup()
        self._jobs: set[asyncio.Task] = set()

    @asynccontextmanager
    async def running(self) -> AsyncIterator[LeaseWork]:
        renewal = None
        shutdown = None
        try:
            async with self._children:
                renewal = asyncio.create_task(self._renew_while_progressing())
                try:
                    yield self
                    while self._jobs:
                        await asyncio.wait(tuple(self._jobs))
                except BaseException:
                    self._service.end(self.scope.task)
                    shutdown = asyncio.create_task(self.resources.aclose())
                    raise
        except BaseException:
            if not await self._join_cleanup(renewal, shutdown, exc_info()):
                raise
        else:
            await self._join_cleanup(renewal, shutdown, (None, None, None))

    async def _join_cleanup(
        self, renewal: asyncio.Task | None, shutdown: asyncio.Task | None, error: ExceptionState
    ) -> bool:
        self._service.end(self.scope.task)
        cleanup = asyncio.create_task(self._cleanup(renewal, shutdown, error))
        cancelled = None
        # The owner remains registered with retirement until this task settles.
        while True:
            try:
                suppressed = await asyncio.shield(cleanup)
                break
            except asyncio.CancelledError as exc:
                if cleanup.cancelled():
                    raise
                cancelled = exc
        if cancelled is not None:
            raise cancelled
        return suppressed

    async def _cleanup(
        self, renewal: asyncio.Task | None, shutdown: asyncio.Task | None, error: ExceptionState
    ) -> bool:
        try:
            try:
                if renewal is not None:
                    renewal.cancel()
                    with suppress(asyncio.CancelledError):
                        await renewal
            finally:
                if shutdown is None:
                    await self.resources.aclose()
                else:
                    await shutdown
        except BaseException:
            if not await self.finalizers.__aexit__(*exc_info()):
                raise
            return True
        return await self.finalizers.__aexit__(*error)

    def _check(self) -> None:
        if self.scope.closed:
            raise VaultClosed("the task scope is closed")
        self.scope.require(*self.scope.refs[0])

    def start(self, operation: Callable[[], Coroutine[Any, Any, T]]) -> asyncio.Task[T]:
        self._check()
        child = self._children.create_task(self.step(operation))
        self._jobs.add(child)
        child.add_done_callback(self._jobs.discard)
        return child

    async def step(self, operation: Callable[[], Awaitable[T]]) -> T:
        self._check()
        result = await operation()
        self._check()
        self._progress.set()
        return result

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[asyncpg.Connection]:
        self._check()
        async with self._service._fence(check=self._check) as connection:
            self._check()
            yield connection
            self._check()
        self._progress.set()

    async def dispatch(self, send: Callable[[], Awaitable[T]]) -> T:
        """Settle a cancellable, already authorized delivery under the state fence."""
        async with self.transaction():
            return await send()

    async def _renew_while_progressing(self) -> None:
        ticket = self._initial
        try:
            while True:
                await asyncio.sleep(max(0, ticket.renewal_at - self._service._monotonic()))
                await self._progress.wait()
                ticket = await self._service._renew(self.scope.task, ticket)
                await ticket.wait()
                self._progress.clear()
        except (VaultProtocolError, VaultError):
            # The current lease retains its original deadline on renewal failure.
            return
