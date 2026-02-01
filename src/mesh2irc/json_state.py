#!/usr/bin/env python3

import dataclasses
import json
from pathlib import Path
from typing import Any

import platformdirs

from mesh2irc.chatter import MessageId

default_state_path = platformdirs.user_state_path("mesh2irc").joinpath("state.json")


@dataclasses.dataclass(frozen=True)
class Config:
    state_path: Path = default_state_path

    @staticmethod
    def from_data(data: dict[str, Any]):
        kwargs = data.copy()
        if "state_path" in data:
            kwargs["state_path"] = Path(data["state_path"])
        return Config(**kwargs)


class JsonState:

    def __init__(self, config: Config) -> None:
        self.config = config
        self.message_ids = set[MessageId]()
        self.dirty = False
        self.config.state_path.parent.mkdir(parents=True, exist_ok=True)

    def is_message_id_marked(self, message_id: MessageId) -> bool:
        return message_id in self.message_ids

    def mark_message_id(self, message_id: MessageId):
        if message_id in self.message_ids:
            return False
        self.message_ids.add(message_id)
        self.dirty = True
        return True

    def commit(self):
        if not self.dirty:
            return
        state_data = {
            "marked_message_ids": [str(e) for e in self.message_ids],
        }
        text = json.dumps(state_data)
        self.config.state_path.write_text(text)
        self.dirty = False

    def load(self):
        state_data: dict[str, Any]
        try:
            state_data = json.loads(self.config.state_path.read_text())
        except FileNotFoundError:
            state_data = {}
        for message_id in [MessageId(e) for e in state_data.get("marked_message_ids", [])]:
            self.mark_message_id(message_id)
