#!/usr/bin/env python3


import asyncio
import logging
import logging.config
import signal
from argparse import ArgumentParser
from asyncio import Task, TaskGroup, AbstractEventLoop
from pathlib import Path
from typing import Any, Optional

import yaml
from meshcore import MeshCore, EventType
from meshcore.events import Event

from mesh2irc.chatter import Chatter
from mesh2irc.common import (
    Contact,
    Channel,
    ContactName,
    PublicKey,
    PublicKeyPrefix,
    ChannelName,
    Message,
    MessageId,
    ContactType,
)
from mesh2irc.config import Config, default_config_path, MeshCoreConfig, MeshCoreDriver
from mesh2irc.matrix.app import MatrixASChatter

logger = logging.getLogger(__name__)


async def get_meshcore(config: MeshCoreConfig, task: Task[Any]):
    if config.driver == MeshCoreDriver.SERIAL:
        assert config.serial_device_path is not None
        meshcore = await MeshCore.create_serial(  # pyright: ignore [reportUnknownMemberType]
            str(config.serial_device_path), auto_reconnect=True
        )
    elif config.driver == MeshCoreDriver.TCP:
        meshcore = await MeshCore.create_tcp(  # pyright: ignore [reportUnknownMemberType]
            config.tcp_endpoint[0], config.tcp_endpoint[1], auto_reconnect=True
        )
    else:
        raise Exception(f"Unknown driver: {config.driver}")

    # TODO I disabled this because I enabled auto_reconnect above, but for some reason the flow never kicks in again
    # so i am enabling again until i figure that out.
    async def disconnect_cb(_event: Event):
        logger.info(f"Serial Disconnected: {_event}")
        task.cancel()

    meshcore.subscribe(EventType.DISCONNECTED, disconnect_cb)

    return meshcore


class MeshCorePlus:
    def __init__(self, meshcore: MeshCore):
        self.meshcore = meshcore
        self.cached_contacts: Optional[set[Contact]] = None
        self.channels = set[Channel]()

    async def iter_channels(self):
        for i in range(40):
            channel = await self.get_channel(idx=i)
            if channel is None:
                continue
            yield channel

    async def get_channel(self, *, idx: Optional[int] = None, name: Optional[ChannelName] = None):

        async def get_channel_by_idx(_idx: int):
            _found = next(filter(lambda x: x.idx == _idx, self.channels), None)
            if _found is not None:
                return _found
            _channel = await self.meshcore.commands.get_channel(_idx)
            if _channel.type == EventType.ERROR:
                raise Exception(f"get_channel({_idx}) error: {_channel}")
            _channel_name_raw = _channel.payload["channel_name"]
            if _channel_name_raw == "":
                return None
            _channel = Channel(ChannelName(_channel_name_raw), _channel.payload["channel_idx"])
            self.channels.add(_channel)
            return _channel

        if idx is not None:
            return await get_channel_by_idx(idx)
        elif name is not None:
            found = next(filter(lambda x: x.name == name, self.channels), None)
            if found is not None:
                return found
            for i in range(40):
                channel = await get_channel_by_idx(i)
                if (channel is not None) and (channel.name == name):
                    return channel
            return None
        raise Exception("Missing channel key")

    async def get_contacts(self):
        if self.cached_contacts is None:
            _cached_contacts: set[Contact] = set()
            _contacts = await self.meshcore.commands.get_contacts()
            if _contacts.type == EventType.ERROR:
                raise Exception(f"get_contacts error: {_contacts}")
            for _contact in _contacts.payload.values():
                _name = ContactName(_contact["adv_name"])
                _public_key = PublicKey(_contact["public_key"])
                _type = _contact["type"]
                _cached_contacts.add(Contact(_name, _public_key, _type))
            self.cached_contacts = _cached_contacts
        return self.cached_contacts

    async def list_contacts(self):
        for contact in await self.get_contacts():
            yield contact

    async def get_contact(
        self,
        *,
        name: Optional[ContactName] = None,
        public_key: Optional[PublicKey] = None,
        public_key_prefix: Optional[PublicKeyPrefix] = None,
    ) -> Optional[Contact]:
        if name is not None:
            return next(filter(lambda x: x.name == name, await self.get_contacts()), None)
        elif public_key is not None:
            return next(filter(lambda x: x.public_key == public_key, await self.get_contacts()), None)
        elif public_key_prefix is not None:
            return next(filter(lambda x: x.public_key.startswith(public_key_prefix), await self.get_contacts()), None)
        raise Exception("Missing contact key")


async def main_loop(config: Config, self_contact: Contact, mcp: MeshCorePlus, chatter: Chatter):

    async def drive_messages():
        while True:
            result = await mcp.meshcore.commands.get_msg()
            if (result.type == EventType.NO_MORE_MSGS) or (result.type == EventType.ERROR):
                break
            message_type = result.payload["type"]
            if message_type == "CHAN":
                user_name_raw, rest = str(result.payload["text"]).split(":", 1)
                contact_name = ContactName(user_name_raw)
                message = Message(rest.lstrip())
                channel = await mcp.get_channel(idx=result.payload["channel_idx"])
                if channel is None:
                    logger.warning(f"Unknown channel: {result}")
                else:
                    await chatter.send_channel(self_contact, contact_name, message, result, channel.name)
            elif message_type == "PRIV":
                public_key_prefix = PublicKeyPrefix(result.payload["pubkey_prefix"])
                contact = await mcp.get_contact(public_key_prefix=public_key_prefix)
                logger.debug(f"Private Message: {result} {contact}")
                if contact is None:
                    logger.warning(f"Unknown contact: {result}")
                else:
                    message = Message(result.payload["text"])
                    await chatter.send_direct(contact, self_contact.name, message, result)
            else:
                raise Exception(f"Unknown message type: {message_type}")

    loop_f = asyncio.Future[None]()

    async def messages_waiting(event: Event):
        try:
            await drive_messages()
        except Exception as e:
            if loop_f.done():
                logger.exception(e)
            else:
                loop_f.set_exception(e)

    mcp.meshcore.subscribe(EventType.MESSAGES_WAITING, messages_waiting)

    async def handle_advertisement(_event: Event):
        public_key = PublicKey(_event.payload["public_key"])
        contact = await mcp.get_contact(public_key=public_key)
        if contact is None:
            advertise = True
        else:
            advertise = config.advertise_known
        if advertise:
            await chatter.advertise(self_contact, public_key, contact=contact)

    mcp.meshcore.subscribe(EventType.ADVERTISEMENT, handle_advertisement)

    async def handle_new_contact(_event: Event):
        public_key = PublicKey(_event.payload["public_key"])
        contact = await mcp.get_contact(public_key=public_key)
        if contact is not None:
            await chatter.update_contact(contact)

    mcp.meshcore.subscribe(EventType.NEW_CONTACT, handle_new_contact)

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

    try:
        logging_config_data = yaml.load(config.logging_config_path.read_text(), Loader=yaml.FullLoader)
        logging.config.dictConfig(logging_config_data)
    except FileNotFoundError:
        pass

    if config.loglevel is not None:
        logger.setLevel(config.loglevel)
    logger.debug(f"Config: {config}")

    meshcore = await get_meshcore(config.meshcore, main_task)
    self_contact_name = ContactName(meshcore.self_info["name"])
    self_contact = Contact(self_contact_name, PublicKey(meshcore.self_info["public_key"]), ContactType.CLIENT)
    chatter: Chatter = MatrixASChatter(config.matrix)
    await chatter.init(self_contact)
    mcp = MeshCorePlus(meshcore)

    async def channel_callback(
        identity: Contact, source: ContactName, channel_name: ChannelName, message: Message, message_id: MessageId
    ):
        if source != self_contact_name:
            logger.debug(f"!send message {message} {message_id}")
            return
        channel = await mcp.get_channel(name=channel_name)
        if channel is None:
            raise Exception(f"Unknown channel: {channel_name}")
        if config.enable_send:
            result = await meshcore.commands.send_chan_msg(  # pyright: ignore [reportUnknownMemberType]
                channel.idx, str(message)
            )
            if result.type == EventType.ERROR:
                raise Exception(result.payload)
        else:
            logger.debug(f"!send message {message} {message_id}")

    async def direct_callback(source: ContactName, destination: PublicKey, message: Message, message_id: MessageId):
        if source != self_contact_name:
            logger.warning(f"!send message {message} {message_id}")
            return
        if len(message) > 156:
            raise Exception(f"Message too long: len[{len(message)}] > {156}")
        if config.enable_send:
            logger.info(f"Direct message: {destination} -> {message}")
            result = await meshcore.commands.send_msg(str(destination), str(message))
            if result.type == EventType.ERROR:
                raise Exception(result.payload)
        else:
            logger.debug(f"!send message {message} {message_id}")

    # await chatter.add_channel_callback(channel_callback)
    await chatter.add_direct_callback(direct_callback)

    async def seed_contacts():
        async with TaskGroup() as g:
            s = asyncio.Semaphore(8)
            async for contact in mcp.list_contacts():
                if contact.type != ContactType.CLIENT:
                    continue

                async def _update_contact(_contact: Contact):
                    async with s:
                        await chatter.update_contact(_contact)

                g.create_task(_update_contact(_contact=contact))

    if config.seed_contacts:
        await seed_contacts()

    async for channel in mcp.iter_channels():
        await chatter.add_channel_callback(self_contact, channel.name, channel_callback)

    async with TaskGroup() as g:
        g.create_task(chatter.run())
        g.create_task(main_loop(config, self_contact, mcp, chatter))


def main():
    asyncio.run(amain())
