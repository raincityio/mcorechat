#!/usr/bin/env python3

import dataclasses
import logging
from pathlib import Path
from typing import Any

import platformdirs

from mesh2irc import json_state
from .matrix.config import Config as MatrixConfig

default_config_path = platformdirs.user_config_path("mesh2chat.yaml")


@dataclasses.dataclass(frozen=True)
class Config:
    serial_device_path: Path
    matrix: MatrixConfig
    loglevel: int = logging.INFO
    json_state_config: json_state.Config = json_state.Config()

    @staticmethod
    def from_data(data: dict[str, Any]) -> "Config":
        kwargs = data.copy()
        if "matrix" in data:
            kwargs["matrix"] = MatrixConfig.from_data(data["matrix"])
        if "serial_device_path" in data:
            kwargs["serial_device_path"] = Path(data["serial_device_path"])
        if "loglevel" in data:
            kwargs["loglevel"] = logging.getLevelName(data["loglevel"])  # pyright: ignore [reportDeprecated]
        if "json_state_config" in data:
            kwargs["json_state_config"] = json_state.Config.from_data(data["json_state_config"])
        return Config(**kwargs)
