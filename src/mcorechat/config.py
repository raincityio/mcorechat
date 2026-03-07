#!/usr/bin/env python3

import dataclasses
import enum
import functools
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

import platformdirs

from .common import ChannelName, config_path_helper
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
    def from_data(root: Path, data: dict[str, Any]):
        field_types: dict[str, Callable[[Any], Any]] = {
            "serial_device_path": functools.partial(config_path_helper, root),
            "mc_endpoint": tuple,
            "driver": MeshCoreDriver,
        }
        kwargs = data.copy()
        for key, cls in field_types.items():
            if key in data:
                kwargs[key] = cls(data[key])
        return MeshCoreConfig(**kwargs)


@dataclasses.dataclass(frozen=True)
class RadioConfig:
    enabled: bool = True
    meshcore: MeshCoreConfig = MeshCoreConfig()

    @staticmethod
    def from_data(root: Path, data: dict[str, Any]) -> "RadioConfig":
        field_types: dict[str, Callable[[Any], Any]] = {
            "meshcore": functools.partial(MeshCoreConfig.from_data, root),
        }
        kwargs = data.copy()
        for key, cls in field_types.items():
            if key in data:
                kwargs[key] = cls(data[key])
        return RadioConfig(**kwargs)


@dataclasses.dataclass(frozen=True)
class Config:
    matrix: MatrixConfig
    radios: dict[str, RadioConfig] = dataclasses.field(default_factory=dict[str, RadioConfig])
    loglevel: Optional[int] = None
    logging_config_path: Path = default_logging_config_path
    dev_enable_send: bool = True
    dev_enable_receive: bool = True
    advertise_known: bool = False
    maxish_message_length: int = 156
    advertisements_channel: ChannelName = ChannelName("[advertisements]")
    command_channel: ChannelName = ChannelName("[command]")

    @staticmethod
    def from_data(root: Path, data: dict[str, Any]) -> "Config":
        field_types: dict[str, Callable[[Any], Any]] = {
            "radios": lambda x: {k: RadioConfig.from_data(root, v) for k, v in x.items()},
            "matrix": functools.partial(MatrixConfig.from_data, root),
            "loglevel": logging.getLevelName,
            "logging_config_path": functools.partial(config_path_helper, root),
            "meshcore": functools.partial(MeshCoreConfig.from_data, root),
            "advertisements_channel": ChannelName,
            "command_channel": ChannelName,
        }
        kwargs = data.copy()
        for key, cls in field_types.items():
            if key in data:
                kwargs[key] = cls(data[key])
        return Config(**kwargs)
