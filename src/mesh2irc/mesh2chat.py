#!/usr/bin/env python3


import asyncio
import logging
import signal
from argparse import ArgumentParser
from asyncio import Task, TaskGroup, AbstractEventLoop
from pathlib import Path
from typing import Any, Optional

import yaml
from meshcore import MeshCore, EventType
from meshcore.events import Event

from mesh2irc.chatter import Chatter, Destination
from mesh2irc.common import (
    Contact,
    Channel,
    ContactName,
    PublicKey,
    PublicKeyPrefix,
    ChannelName,
    UserName,
    Message,
    MessageId,
)
from mesh2irc.config import Config, default_config_path
from mesh2irc.json_state import JsonState
from mesh2irc.matrix.matrix_chatter import MatrixChatter


async def get_meshcore(config: Config, task: Task[Any]):
    # meshcore = await MeshCore.create_serial(str(config.serial_device_path))  # pyright: ignore [reportUnknownMemberType]
    meshcore = await MeshCore.create_tcp("pluto.elisha.raincity.io", 5678)

    async def disconnect_cb(_event: Event):
        logging.info(f"Serial Disconnected: {_event}")
        task.cancel()

    meshcore.subscribe(EventType.DISCONNECTED, disconnect_cb)

    return meshcore


class MeshCorePlus:
    def __init__(self, meshcore: MeshCore):
        self.meshcore = meshcore
        self.cached_contacts: Optional[set[Contact]] = None
        self.channels = set[Channel]()

    async def get_channel(self, *, idx: Optional[int] = None, name: Optional[ChannelName] = None):

        async def get_channel_by_idx(_idx: int):
            _found = next(filter(lambda x: x.idx == _idx, self.channels), None)
            if _found is not None:
                return _found
            _channel = await self.meshcore.commands.get_channel(_idx)
            if _channel.type == EventType.ERROR:
                raise Exception(f"get_channel({i}) error: {_channel}")
            _channel_name_raw = _channel.payload["channel_name"]
            if _channel_name_raw == "":
                return None
            if _channel_name_raw == "Public":
                _channel_name_raw = "public"  # TODO this is lame
            _channel = Channel(ChannelName(_channel_name_raw), _channel.payload["channel_idx"])
            self.channels.add(_channel)
            return _channel

        if idx is not None:
            return await get_channel_by_idx(idx)
        elif name is not None:
            found = next(filter(lambda x: x.name == name, self.channels), None)
            if found is not None:
                return found
            for i in range(0xFF):
                channel = await get_channel_by_idx(i)
                if (channel is not None) and (channel.name == name):
                    return channel
            return None
        raise Exception("Missing channel key")

    async def get_contact(
        self,
        *,
        name: Optional[ContactName] = None,
        public_key: Optional[PublicKey] = None,
        public_key_prefix: Optional[PublicKeyPrefix] = None,
    ):

        async def get_contacts():
            if self.cached_contacts is None:
                _cached_contacts: set[Contact] = set()
                _contacts = await self.meshcore.commands.get_contacts()
                if _contacts.type == EventType.ERROR:
                    raise Exception(f"get_contacts error: {_contacts}")
                for _contact in _contacts.payload.values():
                    _name = ContactName(_contact["adv_name"])
                    _public_key = PublicKey(_contact["public_key"])
                    _cached_contacts.add(Contact(_name, _public_key))
                self.cached_contacts = _cached_contacts
            return self.cached_contacts

        if name is not None:
            return next(filter(lambda x: x.name == name, await get_contacts()), None)
        elif public_key is not None:
            return next(filter(lambda x: x.public_key == public_key, await get_contacts()), None)
        elif public_key_prefix is not None:
            return next(filter(lambda x: x.public_key.startswith(public_key_prefix), await get_contacts()), None)
        raise Exception("Missing contact key")


async def main_loop(mcp: MeshCorePlus, chatter: Chatter):

    async def drive_messages():
        while True:
            result = await mcp.meshcore.commands.get_msg()
            if (result.type == EventType.NO_MORE_MSGS) or (result.type == EventType.ERROR):
                break
            message_type = result.payload["type"]
            if message_type == "CHAN":
                user_name_raw, rest = str(result.payload["text"]).split(":", 1)
                user_name = UserName(user_name_raw)
                message = Message(rest.lstrip())
                channel = await mcp.get_channel(idx=result.payload["channel_idx"])
                if channel is None:
                    logging.warning(f"Unknown channel: {result}")
                else:
                    await chatter.send_message(user_name, message, result, channel_name=channel.name)
            elif message_type == "PRIV":
                public_key_prefix = PublicKeyPrefix(result.payload["pubkey_prefix"])
                contact = await mcp.get_contact(public_key_prefix=public_key_prefix)
                logging.debug(f"Private Message: {result} {contact}")
                if contact is None:
                    logging.warning(f"Unknown contact: {result}")
                else:
                    message = Message(result.payload["text"])
                    await chatter.send_message(UserName(contact.name), message, result)
            else:
                raise Exception(f"Unknown message type: {message_type}")

    loop_f = asyncio.Future[None]()

    async def messages_waiting(event: Event):
        try:
            await drive_messages()
        except Exception as e:
            if loop_f.done():
                logging.exception(e)
            else:
                loop_f.set_exception(e)

    mcp.meshcore.subscribe(EventType.MESSAGES_WAITING, messages_waiting)
    await drive_messages()

    await loop_f


async def amain():
    logging.basicConfig(level=logging.INFO)

    main_task = asyncio.current_task()
    assert main_task is not None
    loop = asyncio.get_running_loop()

    loop.add_signal_handler(signal.SIGINT, main_task.cancel)
    loop.add_signal_handler(signal.SIGTERM, main_task.cancel)

    def unhandled(_loop: AbstractEventLoop, _context: Any):
        _loop.default_exception_handler(_context)
        main_task.cancel()

    loop.set_exception_handler(unhandled)

    argparse = ArgumentParser()
    argparse.add_argument("-c", metavar="config", type=Path, default=default_config_path)
    argparse.add_argument("-d", action="store_true", help="enable debug")
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
    logging.getLogger("nio").setLevel(logging.WARNING)
    logging.debug(f"Config: {config}")

    meshcore = await get_meshcore(config, main_task)
    chatter = MatrixChatter(config.matrix)
    state = JsonState(config.json_state_config)
    state.load()
    mcp = MeshCorePlus(meshcore)

    async def message_callback(user: UserName, destination: Destination, message: Message, message_id: MessageId):
        logging.debug(f"Message: {user} {destination} {message}")
        if state.is_message_id_marked(message_id):
            logging.debug(f"Message marked: {message_id}")
            return
        if type(destination) is UserName:
            contact = await mcp.get_contact(name=ContactName(destination.raw))
            if contact is None:
                raise Exception(f"Unknown contact: {destination}")
            result = await meshcore.commands.send_msg(contact.public_key, message)
            logging.debug(f"send message {result}")
        elif type(destination) is ChannelName:
            channel = await mcp.get_channel(name=destination)
            if channel is None:
                raise Exception(f"Unknown channel: {destination}")
            result = await meshcore.commands.send_chan_msg(  # pyright: ignore [reportUnknownMemberType]
                channel.idx, message
            )
            logging.debug(f"send message {result}")
        else:
            raise Exception(f"Unknown destination: {destination}")
        state.mark_message_id(message_id)

    await chatter.add_message_callback(message_callback)

    try:
        async with TaskGroup() as g:
            g.create_task(chatter.run())
            g.create_task(main_loop(mcp, chatter))

            async def commiter():
                while True:
                    await asyncio.sleep(10)
                    state.commit()

            g.create_task(commiter())
    finally:
        state.commit()


def main():
    asyncio.run(amain())
