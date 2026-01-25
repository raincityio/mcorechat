#!/usr/bin/env python3


import asyncio
import logging
from argparse import ArgumentParser
from asyncio import Task, Event
from typing import Any

from meshcore import MeshCore, EventType

from mesh2irc.config import Config


async def get_meshcore(config: Config, task: Task[Any]):
    meshcore = await MeshCore.create_serial(str(config.serial_device_path))

    async def disconnect_cb(_event: Event):
        logging.info(f"Serial Disconnected: {_event}")
        task.cancel()

    meshcore.subscribe(EventType.DISCONNECTED, disconnect_cb)

    return meshcore


async def amain():
    argparse = ArgumentParser()
    subparsers = argparse.add_subparsers(dest="command")

    config = Config()

    main_task = asyncio.current_task()
    assert main_task is not None

    meshcore = await get_meshcore(config, main_task)


def main():
    asyncio.run(amain())
