#!/usr/bin/env python3

from typing import Protocol

from mesh2irc.chatter import MessageId


class State(Protocol):
    def is_message_id_marked(self, message_id: MessageId) -> bool: ...
    def mark_message_id(self, message_id: MessageId) -> bool: ...
