#!/usr/bin/env python3

import dataclasses


@dataclasses.dataclass(frozen=True)
class ChannelEntry:
    name: str


@dataclasses.dataclass(frozen=True)
class UserEntry:
    name: str
