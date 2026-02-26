#!/usr/bin/env python3
import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

from aiohttp import ClientConnectorError, ClientConnectionError, ServerDisconnectedError, ClientResponseError

from mcorechat.common import backoff_iter

logger = logging.getLogger(__name__)

P = ParamSpec("P")
T = TypeVar("T")


async def client_attempt[T, **P](
    f: Callable[P, Awaitable[T]],
    *args: P.args,
    **kwargs: P.kwargs,
) -> T:
    backoff = iter(backoff_iter())

    while True:
        try:
            return await f(*args, **kwargs)

        except (
            ClientConnectorError,
            ClientConnectionError,
            ServerDisconnectedError,
        ) as e:
            logging.warning("Endpoint not up yet: %s", e)
            await asyncio.sleep(next(backoff))

        except ClientResponseError as e:
            if e.status in {502, 503, 504}:
                logging.warning("Transient HTTP error %s — retrying", e.status)
                await asyncio.sleep(next(backoff))
            else:
                raise
