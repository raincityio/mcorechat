#!/usr/bin/env python3

import dataclasses
import enum
import logging
from pathlib import Path
from typing import Any, Optional

import platformdirs

from .matrix.config import Config as MatrixConfig

default_config_path = platformdirs.user_config_path("mesh2chat").joinpath("config.yml")
default_logging_config_path = platformdirs.user_config_path("mesh2chat").joinpath("logging.yml")
default_tcp_endpoint = (
    "127.0.0.1",
    1234,
)


class MeshCoreDriver(enum.Enum):
    SERIAL = "serial"
    TCP = "tcp"


@dataclasses.dataclass(frozen=True)
class MeshCoreConfig:
    driver: MeshCoreDriver = MeshCoreDriver.TCP
    serial_device_path: Optional[Path] = None
    tcp_endpoint: tuple[str, int] = default_tcp_endpoint

    @staticmethod
    def from_data(data: dict[str, Any]):
        kwargs = data.copy()
        if "serial_device_path" in data:
            kwargs["serial_device_path"] = Path(data["serial_device_path"]).expanduser()
        if "mc_endpoint" in data:
            kwargs["mc_endpoint"] = tuple(data["mc_endpoint"])
        if "driver" in data:
            kwargs["driver"] = MeshCoreDriver(data["driver"])
        return MeshCoreConfig(**kwargs)


@dataclasses.dataclass(frozen=True)
class Config:
    matrix: MatrixConfig
    meshcore: MeshCoreConfig = MeshCoreConfig()
    loglevel: Optional[int] = None
    logging_config_path: Path = default_logging_config_path
    dev_enable_send: bool = True
    advertise_known: bool = False
    maxish_message_length: int = 156

    @staticmethod
    def from_data(data: dict[str, Any]) -> "Config":
        kwargs = data.copy()
        if "matrix" in data:
            kwargs["matrix"] = MatrixConfig.from_data(data["matrix"])
        if "loglevel" in data:
            kwargs["loglevel"] = logging.getLevelName(data["loglevel"])  # pyright: ignore [reportDeprecated]
        if "logging_config_path" in data:
            kwargs["logging_config_path"] = Path(data["logging_config_path"]).expanduser()
        if "meshcore" in data:
            kwargs["meshcore"] = MeshCoreConfig.from_data(data["meshcore"])
        return Config(**kwargs)
