#!/usr/bin/env python3


import asyncio
import json
import logging
import logging.config
import shlex
import signal
from argparse import ArgumentError, ArgumentParser
from asyncio import Task, TaskGroup, AbstractEventLoop, CancelledError
from pathlib import Path
from typing import Any, Optional

import yaml
from meshcore import MeshCore, EventType
from meshcore.events import Event

from mcorechat.chatter import ChatterManager, Chatter, DirectCallback
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
    JSONEncoder,
    HTMLMessage,
    ChannelDisplayName,
)
from mcorechat.config import Config, default_config_path, MeshCoreConfig, MeshCoreDriver
from mcorechat.matrix.app import MatrixChatterManager

logger = logging.getLogger(__name__)

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

    async def _get_channels(self, *, recache: bool | None = None) -> dict[int, Channel]:
        recache = False if recache is None else recache
        if recache or (self.channel_cache is None):
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
        for channel in (await self._get_channels(recache=True)).values():
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


class ThrowingArgumentParser(ArgumentParser):

    # def print_usage(self, file=None):
    #     if file is None:
    #         file = _sys.stdout
    #     super()._print_message(self.format_usage(), file)
    #
    # def print_help(self, file=None):
    #     if file is None:
    #         file = _sys.stdout
    #     super()._print_message(self.format_help(), file)

    def error(self, message: str):
        raise ArgumentError(None, message)

    def exit(self, status: int = 0, message: str | None = None):
        if message:
            raise RuntimeError(message)
        raise RuntimeError(f"Exited with status {status}")


def contact_filter(_contact: Contact):
    return _contact.type == ContactType.CLIENT


class RadioChatter:

    def __init__(self, config: Config, mcp: MeshCorePlus, identity: Contact, chatter: Chatter):
        self.config = config
        self.mcp = mcp
        self.identity = identity
        self.chatter = chatter
        self.lock = asyncio.Lock()
        self.added_public_keys: list[PublicKey] = []
        self.added_channel_names: list[ChannelName] = []

    async def init(self):
        contacts: list[Contact] = []
        async for contact in self.mcp.iter_contacts():
            if contact_filter(contact):
                contacts.append(contact)

        channels: list[Channel] = []
        async for channel in self.mcp.iter_channels():
            if channel not in channels:
                channels.append(channel)

        await self.chatter.prune_contacts(contacts)
        for contact in contacts:
            await self.add_contact(contact)
        for channel in channels:
            await self.add_channel(channel)

        async def handle_advertisements(*_: Any):
            raise InvalidRequestException(f"Advertisements does not support messaging")

        await self.chatter.add_channel(self.config.advertisements_channel, callback=handle_advertisements)
        await self.chatter.add_channel(self.config.command_channel, callback=self.command_callback)

    async def send_advertisement(self, source: Contact | ChannelDisplayName, public_key: PublicKey):
        await self.chatter.send_channel(source, self.config.advertisements_channel, Message(str(public_key)))

    async def add_contact(self, contact: Contact):
        async with self.lock:
            if contact.public_key in self.added_public_keys:
                return
            await self.chatter.add_contact(contact, self.direct_callback(contact))
            self.added_public_keys.append(contact.public_key)

    async def remove_contact(self, contact: Contact):
        async with self.lock:
            if contact.public_key not in self.added_public_keys:
                return
            await self.chatter.remove_contact(contact)
            self.added_public_keys.remove(contact.public_key)

    async def add_channel(self, channel: Channel):
        async with self.lock:
            if channel.name in self.added_channel_names:
                return
            await self.chatter.add_channel(channel.name, self.channel_callback(channel))
            self.added_channel_names.append(channel.name)

    async def remove_channel(self, channel: Channel):
        async with self.lock:
            if channel.name not in self.added_channel_names:
                return
            await self.chatter.remove_channel(channel.name)
            self.added_channel_names.remove(channel.name)

    async def send_error(self, channel_name: ChannelName, msg: str, details: str | None = None) -> None:
        assert details is not None
        await self.chatter.send_channel(
            self.identity,
            channel_name,
            HTMLMessage(f"<i><b>{msg}</b>: {details}</i>"),
        )

    async def command_callback(self, message: Message, message_id: MessageId):
        cmd = shlex.split(str(message))

        parser = ThrowingArgumentParser(exit_on_error=False)
        subparsers = parser.add_subparsers(dest="command")
        subparsers.add_parser("self-info")
        subparser = subparsers.add_parser("send-advert")
        subparser.add_argument("--flood", action="store_true")

        subparser = subparsers.add_parser("set-channel")
        subparser.add_argument("--name", required=True)
        subparser.add_argument("--idx", type=int, required=True)
        subparser = subparsers.add_parser("get-channel")
        subparser.add_argument("--name")
        subparser.add_argument("--idx", type=int)
        subparser = subparsers.add_parser("clear-channel")
        subparser.add_argument("--idx", type=int, required=True)
        subparser = subparsers.add_parser("list-channels")

        subparser = subparsers.add_parser("add-contact")
        subparser = subparsers.add_parser("get-contact")
        subparser = subparsers.add_parser("remove-contact")
        subparser = subparsers.add_parser("list-contacts")
        try:
            args = parser.parse_args(args=cmd)
        except ArgumentError as e:
            raise InvalidRequestException(str(e)) from e

        if args.command is None:
            raise InvalidRequestException(f"Invalid command: {cmd}")
        elif args.command == "self-info":
            output = [json.dumps(self.mcp.meshcore.self_info)]
        elif args.command == "get-channel":
            if args.name:
                channel = await self.mcp.get_channel(name=ChannelName(args.name))
            else:
                channel = await self.mcp.get_channel(idx=args.idx)
            output = [json.dumps(channel, cls=JSONEncoder)]
        elif args.command == "send-advert":
            advert = await self.mcp.meshcore.commands.send_advert(flood=args.flood)
            output = [json.dumps(advert, cls=JSONEncoder)]
        elif args.command == "set-channel":
            channel_name = ChannelName(args.name)
            current_channel = await self.mcp.get_channel(name=channel_name)
            if current_channel is not None:
                await self.remove_channel(current_channel)
            set_channel_result = await self.mcp.meshcore.commands.set_channel(args.idx, args.name)
            channel = Channel(channel_name, args.idx)
            await self.add_channel(channel)
            output = [json.dumps(set_channel_result, cls=JSONEncoder)]
        elif args.command == "clear-channel":
            current_channel = await self.mcp.get_channel(idx=args.idx)
            if current_channel is not None:
                await self.remove_channel(current_channel)
            result = await self.mcp.meshcore.commands.set_channel(args.idx, "", bytes.fromhex(16 * "00"))
            output = [json.dumps(result, cls=JSONEncoder)]
        elif args.command == "list-channels":
            channels: list[Channel] = []
            async for channel in self.mcp.iter_channels():
                channels.append(channel)
            output = [json.dumps(channels, cls=JSONEncoder)]
        else:
            raise InvalidRequestException(f"Unknown command: {cmd}")
        for line in output:
            await self.chatter.send_channel(self.identity, self.config.command_channel, Message(line))

    def channel_callback(self, channel: Channel):
        async def callback(message: Message, message_id: MessageId) -> None:
            if len(message) > self.config.maxish_message_length:
                await self.send_error(
                    channel.name, f"Message too long", f"len[{len(message)}] > {self.config.maxish_message_length}"
                )
                return
            if self.config.dev_enable_send:
                logger.debug(f"send message {message} {message_id}")
                await self.mcp.send_chan_msg(channel.idx, message)
            else:
                logger.debug(f"!send message {message} {message_id}")

        return callback

    def direct_callback(self, contact: Contact) -> DirectCallback:
        async def callback(message: Message, message_id: MessageId):
            if len(message) > self.config.maxish_message_length:
                raise InvalidRequestException(
                    f"Message too long: len[{len(message)}] > {self.config.maxish_message_length}"
                )
            if self.config.dev_enable_send:
                logger.debug(f"send message {contact} {message}")
                await self.mcp.send_msg(contact.public_key, message)
            else:
                logger.debug(f"!send message {contact} {message}")

        return callback

    async def run(self) -> None:

        # start listening for radio events
        async def handle_contact_msg_recv(event: Event):
            public_key_prefix = PublicKeyPrefix(event.payload["pubkey_prefix"])
            contact = await self.mcp.get_contact(public_key_prefix=public_key_prefix)
            logger.debug(f"Private Message: {event} {contact}")
            if contact is None:
                logger.warning(f"Unknown contact: {event}")
            else:
                message = Message(event.payload["text"])
                await self.chatter.send_direct(contact, message)

        async def handle_channel_msg_recv(event: Event):
            user_name_raw, rest = str(event.payload["text"]).split(":", 1)
            channel_display_name = ChannelDisplayName(user_name_raw)
            message = Message(rest.lstrip())
            channel = await self.mcp.get_channel(idx=event.payload["channel_idx"])
            if channel is None:
                logger.warning(f"Unknown channel: {event}")
            else:
                await self.chatter.send_channel(channel_display_name, channel.name, message)

        handle_messages_waiting_l = asyncio.Lock()

        async def handle_messages_waiting(*_: Any):
            async with handle_messages_waiting_l:
                while True:
                    result = await self.mcp.meshcore.commands.get_msg()
                    if result.type == EventType.NO_MORE_MSGS:
                        break
                    elif result.type == EventType.CHANNEL_MSG_RECV:
                        await handle_channel_msg_recv(result)
                    elif result.type == EventType.CONTACT_MSG_RECV:
                        await handle_contact_msg_recv(result)
                    else:
                        raise Exception(f"Unexpected event: {result}")

        async def handle_advertisement(_event: Event):
            public_key = PublicKey(_event.payload["public_key"])
            contact = await self.mcp.get_contact(public_key=public_key)
            if contact is None:
                advertise = True
            else:
                advertise = self.config.advertise_known
            if advertise:
                if contact is None:
                    source = ChannelDisplayName("Unknown")
                else:
                    source = contact
                await self.send_advertisement(source, public_key)

        async def handle_new_contact(_event: Event):
            public_key = PublicKey(_event.payload["public_key"])
            contact = await self.mcp.get_contact(public_key=public_key)
            if contact is not None:
                if contact_filter(contact):
                    await self.add_contact(contact)

        async def handle_event(event: Event):
            logger.debug(f"Processing event: {event}")
            match event.type:
                case EventType.MESSAGES_WAITING:
                    await handle_messages_waiting()
                case EventType.ADVERTISEMENT:
                    await handle_advertisement(event)
                case EventType.NEW_CONTACT:
                    await handle_new_contact(event)
                case _:
                    logger.warning(f"Unexpected event: {event}")

        self.mcp.meshcore.subscribe(EventType.MESSAGES_WAITING, handle_event)
        self.mcp.meshcore.subscribe(EventType.ADVERTISEMENT, handle_event)
        self.mcp.meshcore.subscribe(EventType.NEW_CONTACT, handle_event)
        # on connected (reconnected) check for messages
        self.mcp.meshcore.subscribe(EventType.CONNECTED, handle_messages_waiting)

        await handle_messages_waiting()
        await asyncio.Event().wait()


async def create_radio_chatter(
    main_task: asyncio.Task[None], config: Config, meshcore_config: MeshCoreConfig, chatter_manager: ChatterManager
) -> RadioChatter:
    meshcore = await get_meshcore(meshcore_config, main_task)
    mcp = MeshCorePlus(meshcore)
    self_contact = Contact(
        ContactName(mcp.meshcore.self_info["name"]), PublicKey(mcp.meshcore.self_info["public_key"]), ContactType.CLIENT
    )
    chatter = await chatter_manager.add_chatter(
        self_contact,
    )
    radio_chatter = RadioChatter(config, mcp, self_contact, chatter)
    await radio_chatter.init()
    return radio_chatter


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
    config = Config.from_data(args.c.parent, config_data)

    try:
        logging_config_data = yaml.load(config.logging_config_path.read_text(), Loader=yaml.FullLoader)
        logging.config.dictConfig(logging_config_data)
    except FileNotFoundError:
        pass

    if config.loglevel is not None:
        logger.setLevel(config.loglevel)
    logger.debug(f"Config: {config}")

    chatter_manager: ChatterManager = MatrixChatterManager(config.matrix)

    radio_chatters: list[RadioChatter] = []
    for radio_config in config.radios.values():
        if radio_config.enabled:
            radio_chatters.append(await create_radio_chatter(main_task, config, radio_config.meshcore, chatter_manager))

    async with TaskGroup() as g:
        for radio_chatter in radio_chatters:
            g.create_task(radio_chatter.run())
        g.create_task(chatter_manager.run())


def main():
    try:
        asyncio.run(amain())
    except CancelledError:
        logger.info(f"Cancelled")
