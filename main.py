from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from traceback import format_exc

import aiohttp
import crescent
from dotenv import load_dotenv

load_dotenv('config.env')
bot = crescent.Bot(os.getenv('discord_token'))


def create_logger(module_name: str, level: int | str = logging.INFO) -> logging.Logger:
    """
    Creates logger and returns an instance of logging object.
    :param level: The level for logging. (Default: logging.INFO)
    :param module_name: Logger name that will appear in text.
    :return: Logging Object.
    """
    # Setting up the root logger
    logger = logging.getLogger(module_name)
    logger.setLevel(logging.DEBUG)

    log_stream = logging.StreamHandler()
    log_stream.setLevel(level)
    formatter = logging.Formatter('[%(asctime)s] %(levelname)s [%(name)s.%(funcName)s:%(lineno)d] %(message)s')
    log_stream.setFormatter(formatter)
    logger.addHandler(log_stream)
    logger.propagate = False

    # Limit file size to 5MB
    log_file = RotatingFileHandler("Discord_Trello.log", maxBytes=5 * 1024 * 1024, backupCount=1)
    log_file.setLevel(level)
    formatter = logging.Formatter('[%(asctime)s] %(levelname)s [%(name)s.%(funcName)s:%(lineno)d] %(message)s')
    log_file.setFormatter(formatter)
    logger.addHandler(log_file)
    logger.propagate = False

    return logger


@dataclass
class Config:
    refresh_interval: float
    prev_refresh_interval: float
    channels: dict[int, set[tuple[str, str]]]


async def check_trello_activity():
    """
    Iterate through all boards and checks their activities. All activity since Current Time - Refresh Interval is grabbed and sent to discord server.

    prev_refresh_interval is used because if refresh interval is changed. The effects are applied after one iteration.
    Example: If Refresh Interval is changed from 1 hour to 5 seconds. The next time check_trello_activity triggers is when 1 hour finishes. If we only
    check for last 5 seconds, the bot will miss cards.
    :return:
    """

    global bot_config
    while True:
        drift_time = 0
        for channel, list_of_boards in bot_config.channels.items():
            logger.info(f"Checking trello activity {channel} id.")
            for board in list_of_boards:
                cards = set()
                board_id = re.search(r"b/(.*)/", board[1]).group(1)
                async with aiohttp.ClientSession(headers={"Accept": "application/json"}) as session:
                    query = {
                        'key': os.getenv('TRELLO_API_KEY'),
                        'token': os.getenv('TRELLO_TOKEN'),
                        'filter': 'copyCard,createCard,updateCard',
                        'limit': "1000"
                    }
                    async with session.get(f"https://api.trello.com/1/boards/{board_id}/actions", params=query) as resp:
                        board_actions = await resp.json()
                        if resp.status != 200:
                            break

                        for action in board_actions:
                            # since python datetime doesn't follow ISO standard fully
                            action_time = datetime.fromisoformat(action['date'].replace('Z', ""))
                            diff = datetime.utcnow() - action_time
                            logger.info(f"{action['data']['card']['shortLink']} {datetime.now()} - {action_time} = {diff}.")
                            if diff <= timedelta(seconds=bot_config.prev_refresh_interval):
                                cards.add(f"https://trello.com/c/{action['data']['card']['shortLink']}")

                if cards:
                    await bot.rest.create_message(channel=channel,
                                                  content=f"New/Updated Cards since {datetime.now() - timedelta(seconds=bot_config.prev_refresh_interval):%r} "
                                                          f"{datetime.now().astimezone().tzinfo}")
                    await asyncio.sleep(1)
                    drift_time = len(cards) + 1
                for card in cards:
                    logger.info(f"{card}")
                    await bot.rest.create_message(channel=channel, content=f"{card}")
                    await asyncio.sleep(1)

        bot_config.prev_refresh_interval = bot_config.refresh_interval
        logger.info(f"Sleeping for {bot_config.refresh_interval - drift_time} seconds. Scheduled to run at "
                    f"{datetime.now() + timedelta(seconds=(bot_config.refresh_interval - drift_time))}.")
        await asyncio.sleep(bot_config.refresh_interval - drift_time)


async def get_board(board_id: str):
    """
    Gets board information from Trello.

    :param board_id: Trello Board ID
    :return: dict() object with Board information
    :raises: aiohttp.ClientResponseError
        401: Unauthorized
        404: Not Found
    """
    async with aiohttp.ClientSession(headers={"Accept": "application/json"}) as session:
        query = {
            'key': os.getenv('TRELLO_API_KEY'),
            'token': os.getenv('TRELLO_TOKEN')
        }
        async with session.get(f"https://api.trello.com/1/boards/{board_id}", params=query) as resp:
            resp.raise_for_status()
            return await resp.json()


@bot.include
@crescent.command(description="List all the boards being watched in the current channel.", guild=int(os.getenv('guild_id')))
async def list_boards(ctx: crescent.Context, all_boards: bool = False):
    """
    List all the boards being watched.

    :param ctx: crescent context
    :param all_boards: True returns all boards being watched in Guild otherwise it only returns boards in respective channel
    :return: None
    """

    logger.info(f"list board command {ctx.channel.name} all_boards {all_boards}.")
    if all_boards:
        watched_boards_summary = ""
        for channel_id, list_of_boards in bot_config.channels.items():
            watched_boards_summary = f"{watched_boards_summary}**#{ctx.guild.get_channel(channel_id).name}**\n"
            for board in list_of_boards:
                watched_boards_summary = f"{watched_boards_summary}- {board[0]}\n"
            watched_boards_summary = f"{watched_boards_summary}\n"
        await ctx.respond(f"Currently watching\n{watched_boards_summary}")
    else:
        if bot_config.channels.get(ctx.channel_id):
            boards_in_curr_ch = '\n'.join([f"- {x[0]}" for x in bot_config.channels.get(ctx.channel_id)])
            await ctx.respond(f"Following boards are being watched in current channel\n{boards_in_curr_ch}")
        else:
            await ctx.respond("No boards are being watched in current channel.")


@bot.include
@crescent.command(description="Deletes all the watched boards", guild=int(os.getenv('guild_id')))
async def reset_bot(ctx: crescent.Context):
    """
    Rests the bot to initial state.

    :param ctx: crescent context
    :return: None
    """
    global bot_config

    bot_config.refresh_interval = 3600
    bot_config.channels = dict()
    logger.info("All watches are deleted.")
    await ctx.respond("All watches are deleted.")


@bot.include
@crescent.command(description="Sets the refresh interval (minutes).", guild=int(os.getenv('guild_id')))
async def set_refresh_interval(ctx: crescent.Context, refresh_interval: float):
    """
    Sets the refresh interval. It is the duration after which all boards are polled.

    :param ctx: crescent context
    :param refresh_interval: Interval in minutes
    :return: None
    """
    global bot_config
    bot_config.refresh_interval = refresh_interval * 60  # Converting minutes to seconds
    logger.info(f"Refresh interval is set to {refresh_interval} minutes.")
    await ctx.respond(f"Refresh interval is set to {refresh_interval} minutes.")


@bot.include
@crescent.command(description="Watches the boards and sends the updates in current channel.", guild=int(os.getenv('guild_id')))
async def watch_board(ctx: crescent.Context, board_url: str):
    """
    Adds board to the list of watched boards.

    :param ctx: crescent context
    :param board_url: Trello Board URL
    :return: None
    """
    global bot_config

    logger.info(f"Watch Board command received. URL {board_url}.")
    if not (result := re.search(r"b/(.*)/", board_url)):
        await ctx.respond("Incorrect Board URL")
        return

    for channel_id, list_of_boards in bot_config.channels.items():
        for board in list_of_boards:
            if board[1].lower() == board_url.lower():
                logger.info(f"Board Already Exists {board[1]}.")
                await ctx.respond(f"Board is already being watched in channel #{ctx.guild.get_channel(channel_id).name}")
                return

    board_id = result.group(1)
    try:
        board = await get_board(board_id)
    except aiohttp.ClientResponseError:
        logger.exception("Could not obtain board info.", exc_info=True)
        await ctx.respond(format_exc())
        return

    if boards := bot_config.channels.get(ctx.channel_id):
        boards.add((board.get('name'), board_url))
    else:
        bot_config.channels[ctx.channel_id] = {(board.get('name'), board_url)}

    logger.info(f"Added board {board.get('name')}: {board_url}.")
    await ctx.respond(f"Added board {board.get('name')}: {board_url} to current channel")


def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(check_trello_activity())
    bot.run()


if __name__ == '__main__':
    bot_config = Config(3600, 3600, dict())
    logger = create_logger("main.py")
    main()
