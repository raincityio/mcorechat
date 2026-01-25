#!/usr/bin/env python3

from typing import Protocol, AsyncIterator

from mesh2irc.irc.common import ChannelEntry


class ChannelStore(Protocol):
    async def list(self) -> list[ChannelEntry]: ...


class FakeChannelStore:
    def __init__(self):
        self.channels: list[ChannelEntry] = []

    def list(self) -> AsyncIterator[ChannelEntry]:
        for channel in self.channels:
            yield channel
