#!/usr/bin/env python3
import json

from meshcore.events import Event, EventType


class JSONEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Event):
            data = {
                "type": o.type,
                "payload": o.payload,
                "attributes": o.attributes,
            }
            return data
        elif isinstance(o, EventType):
            return o.name
        elif isinstance(o, bytes):
            return o.hex()
        super().default(o)
