#!/usr/bin/env python3


import asyncio
import json
import logging
import logging.config
import shlex
import signal
from argparse import ArgumentError, ArgumentParser
from asyncio import Future, Task, TaskGroup, AbstractEventLoop, CancelledError
from pathlib import Path
from typing import Any, Optional

import yaml
from aiohttp import ClientError
from meshcore import MeshCore, EventType
from meshcore.events import Event

from mcorechat.chatter import Chatter
from mcorechat.common import (
    Contact,
    Channel,
    ContactName,
    PublicKey,
    PublicKeyPrefix,
    ChannelName,
    Message,
    MessageId,
    ContactType,
    backoff_iter,
    JSONEncoder,
    DisplayName,
)
from mcorechat.config import Config, default_config_path, MeshCoreConfig, MeshCoreDriver
from mcorechat.matrix.app import MatrixASChatter

logger = logging.getLogger(__name__)

MAXISH_MESSAGE_LENGTH = 156
MAX_CHANNELS = 40


class MeshcoreError(Exception):
    def __init__(self, msg: str, event: Event):
        self.event = event
        super().__init__(f"{msg}: {event}")


class InvalidRequestException(Exception):
    def __init__(self, msg: str):
        super().__init__(msg)


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
        self.contact_cache: dict[PublicKey, Contact] | None = None
        self.channel_cache: dict[int, Channel] | None = None

    async def _get_channels(self):
        if self.channel_cache is None:
            channel_cache: dict[int, Channel] = {}
            for idx in range(MAX_CHANNELS):
                mc_channel = await self.meshcore.commands.get_channel(idx)
                if mc_channel.type == EventType.ERROR:
                    raise MeshcoreError(f"get_channel({idx})", mc_channel)
                channel_name_raw = mc_channel.payload["channel_name"]
                if channel_name_raw != "":
                    channel = Channel(ChannelName(channel_name_raw), mc_channel.payload["channel_idx"])
                    channel_cache[idx] = channel
            self.channel_cache = channel_cache
        return self.channel_cache

    async def iter_channels(self):
        for channel in (await self._get_channels()).values():
            yield channel

    async def get_channel(self, *, idx: Optional[int] = None, name: Optional[ChannelName] = None):
        channels = await self._get_channels()
        match (idx, name):
            case (int(), None):
                return channels.get(idx, None)
            case (None, ChannelName()):
                return next(filter(lambda x: x.name == name, channels.values()), None)
            case _:
                raise Exception(f"one of idx or name required")

    async def _get_contacts(self):
        if self.contact_cache is None:
            contact_cache: dict[PublicKey, Contact] = {}
            contacts = await self.meshcore.commands.get_contacts()
            if contacts.type == EventType.ERROR:
                raise MeshcoreError(f"get_contacts", contacts)
            for _contact in contacts.payload.values():
                name = ContactName(_contact["adv_name"])
                public_key = PublicKey(_contact["public_key"])
                type = _contact["type"]
                contact_cache[public_key] = Contact(name, public_key, type)
            self.contact_cache = contact_cache
        return self.contact_cache

    async def iter_contacts(self):
        for contact in (await self._get_contacts()).values():
            yield contact

    async def get_contact(
        self,
        *,
        name: Optional[ContactName] = None,
        public_key: Optional[PublicKey] = None,
        public_key_prefix: Optional[PublicKeyPrefix] = None,
    ) -> Optional[Contact]:
        contacts = await self._get_contacts()
        match (name, public_key, public_key_prefix):
            case (ContactName(), None, None):
                return next(filter(lambda x: x.name == name, contacts.values()), None)
            case (None, PublicKey(), None):
                return contacts.get(public_key, None)
            case (None, None, PublicKeyPrefix()):
                return next(filter(lambda x: x.public_key.startswith(public_key_prefix), contacts.values()), None)
            case _:
                raise Exception(f"one of name or public_key or public_key_prefix required")

    async def send_chan_msg(self, channel_idx: int, message: Message):
        result = await self.meshcore.commands.send_chan_msg(  # pyright: ignore [reportUnknownMemberType]
            channel_idx, str(message)
        )
        if result.type == EventType.ERROR:
            raise MeshcoreError(f"send_chan_msg({channel_idx}, {message})", result.payload)

    async def send_msg(self, destination: PublicKey, message: Message):
        result = await self.meshcore.commands.send_msg(str(destination), str(message))
        if result.type == EventType.ERROR:
            raise MeshcoreError(f"send_msg({destination}, {message})", result.payload)


async def main_loop(config: Config, self_contact: Contact, mcp: MeshCorePlus, chatter: Chatter):

    async def handle_contact_msg_recv(event: Event):
        public_key_prefix = PublicKeyPrefix(event.payload["pubkey_prefix"])
        contact = await mcp.get_contact(public_key_prefix=public_key_prefix)
        logger.debug(f"Private Message: {event} {contact}")
        if contact is None:
            logger.warning(f"Unknown contact: {event}")
        else:
            message = Message(event.payload["text"])
            await chatter.send_direct(self_contact, contact, self_contact.name, message)

    async def handle_channel_msg_recv(event: Event):
        user_name_raw, rest = str(event.payload["text"]).split(":", 1)
        display_name = DisplayName(user_name_raw)
        message = Message(rest.lstrip())
        channel = await mcp.get_channel(idx=event.payload["channel_idx"])
        if channel is None:
            logger.warning(f"Unknown channel: {event}")
        else:
            await chatter.send_channel(self_contact, display_name, message, channel.name)

    async def handle_messages_waiting(event_q: asyncio.Queue[Event]):
        while True:
            result = await mcp.meshcore.commands.get_msg()
            match result.type:
                case EventType.NO_MORE_MSGS:
                    break
                case EventType.CHANNEL_MSG_RECV:
                    event_q.put_nowait(result)
                case EventType.CONTACT_MSG_RECV:
                    event_q.put_nowait(result)
                case _:
                    raise Exception(f"Unexpected event: {result}")

    async def handle_advertisement(_event: Event):
        public_key = PublicKey(_event.payload["public_key"])
        contact = await mcp.get_contact(public_key=public_key)
        if contact is None:
            advertise = True
        else:
            advertise = config.advertise_known
        if advertise:
            await chatter.advertise(self_contact, public_key, contact=contact)

    async def handle_new_contact(_event: Event):
        public_key = PublicKey(_event.payload["public_key"])
        contact = await mcp.get_contact(public_key=public_key)
        assert False  # TODO
        if contact is not None:
            await chatter.add_contact(self_contact, contact, None)

    async def event_loop():
        event_q: asyncio.Queue[Event] = asyncio.Queue()
        mcp.meshcore.subscribe(EventType.MESSAGES_WAITING, event_q.put)
        mcp.meshcore.subscribe(EventType.ADVERTISEMENT, event_q.put)
        mcp.meshcore.subscribe(EventType.NEW_CONTACT, event_q.put)

        await handle_messages_waiting(event_q)
        while True:
            event = await event_q.get()
            backoff = iter(backoff_iter())
            while True:
                logger.debug(f"Processing event: {event}")
                try:
                    match event.type:
                        case EventType.MESSAGES_WAITING:
                            await handle_messages_waiting(event_q)
                        case EventType.ADVERTISEMENT:
                            await handle_advertisement(event)
                        case EventType.NEW_CONTACT:
                            await handle_new_contact(event)
                        case EventType.CONTACT_MSG_RECV:
                            await handle_contact_msg_recv(event)
                        case EventType.CHANNEL_MSG_RECV:
                            await handle_channel_msg_recv(event)
                        case _:
                            logger.warning(f"Unexpected event: {event}")
                    break
                except ClientError as e:
                    logging.error(e)
                    await asyncio.sleep(next(backoff))

    await event_loop()


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
    self_display_name = DisplayName(meshcore.self_info["name"])
    self_contact = Contact(self_contact_name, PublicKey(meshcore.self_info["public_key"]), ContactType.CLIENT)
    chatter: Chatter = MatrixASChatter(config.matrix)
    await chatter.init(self_contact)  # TODO what if synapse is down
    mcp = MeshCorePlus(meshcore)

    fault: Future[None] = Future()

    def fault_wrapper(cb: Any):
        class Helper:
            async def __call__(self, *args: Any, **kwargs: Any):
                try:
                    await cb(*args, **kwargs)
                except InvalidRequestException:
                    raise
                except Exception as e:
                    fault.set_exception(e)

        return Helper()

    class ThrowingArgumentParser(ArgumentParser):
        def error(self, message: str):
            raise ArgumentError(None, message)

        def exit(self, status: int = 0, message: str | None = None):
            if message:
                raise RuntimeError(message)
            raise RuntimeError(f"Exited with status {status}")

    async def command_callback(source: DisplayName, message: Message) -> list[str]:
        cmd = shlex.split(str(message))

        parser = ThrowingArgumentParser(exit_on_error=False)
        subparsers = parser.add_subparsers(dest="command")
        subparsers.add_parser("self-info")
        subparser = subparsers.add_parser("get-channel")
        subparser.add_argument("--name", required=True)
        subparser = subparsers.add_parser("send-advert")
        subparser.add_argument("--flood", action="store_true")
        try:
            args = parser.parse_args(args=cmd)
        except ArgumentError as e:
            raise InvalidRequestException(str(e)) from e

        if args.command is None:
            raise InvalidRequestException(f"Invalid command: {cmd}")
        elif args.command == "self-info":
            return [json.dumps(meshcore.self_info)]
        elif args.command == "get-channel":
            channel = await mcp.get_channel(name=ChannelName(args.name))
            return [json.dumps(channel, cls=JSONEncoder)]
        elif args.command == "send-advert":
            advert = await meshcore.commands.send_advert(flood=args.flood)
            return [json.dumps(advert, cls=JSONEncoder)]
        else:
            raise InvalidRequestException(f"Unknown command: {cmd}")

    @fault_wrapper
    async def channel_callback(source: DisplayName, channel_name: ChannelName, message: Message, message_id: MessageId):
        if source != self_display_name:
            # FIXME illegal state
            logger.debug(f"!send message {message} {message_id}")
            return
        if len(message) > MAXISH_MESSAGE_LENGTH:
            raise InvalidRequestException(f"Message too long: len[{len(message)}] > {MAXISH_MESSAGE_LENGTH}")
        channel = await mcp.get_channel(name=channel_name)
        if channel is None:
            raise InvalidRequestException(f"Unknown channel: {channel_name}")
        if config.enable_send:
            logger.debug(f"send message {message} {message_id}")
            await mcp.send_chan_msg(channel.idx, message)
        else:
            logger.debug(f"!send message {message} {message_id}")

    @fault_wrapper
    async def direct_callback(source: DisplayName, destination: PublicKey, message: Message, message_id: MessageId):
        if source != self_display_name:
            # FIXME illegal state
            logger.warning(f"!send message {message} {message_id}")
            return
        if len(message) > MAXISH_MESSAGE_LENGTH:
            raise InvalidRequestException(f"Message too long: len[{len(message)}] > {MAXISH_MESSAGE_LENGTH}")
        if config.enable_send:
            logger.debug(f"send message {destination} {message}")
            await mcp.send_msg(destination, message)
        else:
            logger.debug(f"!send message {destination} {message}")

    async def seed_contacts():
        async with TaskGroup() as g:
            s = asyncio.Semaphore(16)
            async for contact in mcp.iter_contacts():
                if contact.type != ContactType.CLIENT:
                    continue

                async def _update_contact(_contact: Contact):
                    async with s:
                        await chatter.add_contact(self_contact, _contact, direct_callback)

                g.create_task(_update_contact(_contact=contact))

    # Set up and run
    # await chatter.add_direct_callback(self_display_name, direct_callback)

    async for channel in mcp.iter_channels():
        await chatter.add_channel(self_contact, channel.name, channel_callback)

    await chatter.add_command_callback(self_contact, command_callback)

    # TODO what if synapse is down
    if config.seed_contacts:
        await seed_contacts()

    async with TaskGroup() as g:
        g.create_task(chatter.run())
        g.create_task(main_loop(config, self_contact, mcp, chatter))
        await fault


def main():
    try:
        asyncio.run(amain())
    except CancelledError:
        logger.info(f"Cancelled")
