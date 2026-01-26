#!/usr/bin/env python3

import dataclasses
import logging
from pathlib import Path
from typing import Any

import platformdirs

from .matrix.config import Config as MatrixConfig

default_config_path = platformdirs.user_config_path("mesh2chat.yaml")
default_serial_device_path = Path("/dev/cu.usbmodem2301")


@dataclasses.dataclass(frozen=True)
class Config:
    matrix: MatrixConfig = MatrixConfig()
    serial_device_path: Path = default_serial_device_path
    loglevel: int = logging.INFO

    @staticmethod
    def from_data(data: dict[str, Any]) -> "Config":
        kwargs = data.copy()
        if "matrix" in data:
            kwargs["matrix"] = MatrixConfig.from_data(data["matrix"])
        if "serial_device_path" in data:
            kwargs["serial_device_path"] = Path(data["serial_device_path"])
        if "loglevel" in data:
            kwargs["loglevel"] = logging.getLevelName(data["loglevel"])
        return Config(**kwargs)
