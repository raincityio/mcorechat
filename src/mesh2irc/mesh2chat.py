#!/usr/bin/env python3


import asyncio
import logging
from argparse import ArgumentParser
from asyncio import Task
from pathlib import Path
from typing import Any

import yaml
from meshcore import MeshCore, EventType
from meshcore.events import Event

from mesh2irc.chatter import Chatter, UserName, Message, ChannelName
from mesh2irc.config import Config, default_config_path
from mesh2irc.matrix.matrix_chatter import MatrixChatter


async def get_meshcore(config: Config, task: Task[Any]):
    meshcore = await MeshCore.create_serial(str(config.serial_device_path))

    async def disconnect_cb(_event: Event):
        logging.info(f"Serial Disconnected: {_event}")
        task.cancel()

    meshcore.subscribe(EventType.DISCONNECTED, disconnect_cb)

    return meshcore


async def main_loop(config: Config, meshcore: MeshCore, chatter: Chatter):

    channel_infos = dict[int, ChannelName]()

    async def get_channel_name(_channel_idx: int):
        if _channel_idx == 0:
            return "public"
        if _channel_idx in channel_infos:
            return channel_infos[_channel_idx]
        _channel_info = await meshcore.commands.get_channel(_channel_idx)
        print(_channel_info)
        return ChannelName("foo")

    async def drive_messages():
        while True:
            result = await meshcore.commands.get_msg()
            logging.debug(result)
            if (result.type == EventType.NO_MORE_MSGS) or (result.type == EventType.ERROR):
                break

            user_name_raw, rest = str(result.payload["text"]).split(":", 1)
            user_name = UserName(user_name_raw)
            message = Message(rest.lstrip())
            channel_name = await get_channel_name(result.payload["channel_idx"])
            await chatter.send_message(user_name, message, channel_name=channel_name)
            # break

    loop_f = asyncio.Future[None]()

    async def messages_waiting(event: Event):
        try:
            await drive_messages()
        except Exception as e:
            if loop_f.done():
                logging.exception(e)
            else:
                loop_f.set_exception(e)

    meshcore.subscribe(EventType.MESSAGES_WAITING, messages_waiting)
    await drive_messages()

    await loop_f


async def amain():
    argparse = ArgumentParser()
    argparse.add_argument("-c", metavar="config", type=Path, default=default_config_path)
    argparse.add_argument("-d", action="store_true", help="enable debug")
    # subparsers = argparse.add_subparsers(dest="command")
    args = argparse.parse_args()

    config_data: dict[str, Any]
    try:
        config_data = yaml.load(args.c.read_text(), Loader=yaml.FullLoader)
    except FileNotFoundError:
        config_data = {}
    if args.d:
        config_data["loglevel"] = "DEBUG"
    config = Config.from_data(config_data)
    logging.root.setLevel(config.loglevel)
    logging.debug(f"Config: {config}")

    main_task = asyncio.current_task()
    assert main_task is not None

    meshcore = await get_meshcore(config, main_task)
    chatter = MatrixChatter(config.matrix)
    await main_loop(config, meshcore, chatter)


def main():
    asyncio.run(amain())
