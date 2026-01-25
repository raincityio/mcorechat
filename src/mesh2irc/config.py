#!/usr/bin/env python3

import dataclasses
import logging
from pathlib import Path

import platformdirs

from .matrix.config import Config as MatrixConfig

default_config_path = platformdirs.user_config_path("mesh2chat.yaml")
default_serial_device_path = Path("/dev/cu.usbmodem2301")


@dataclasses.dataclass(frozen=True)
class Config:
    matrix: MatrixConfig = MatrixConfig()
    serial_device_path: Path = default_serial_device_path
    loglevel: int = logging.INFO
