"""
Initialize and install the secret santa database.
Otherwise, do nothing.
"""

import os
import sys
import asyncio
import argparse

if __name__ == "__main__":
    # Import config
    import config

    # Import the schema
    import secretsanta
    import teamsbot

    # Import the DB connection
    from database import Base, engine
    from botframework.connector.aio import ConnectorClient
    from botframework.connector.auth import MicrosoftAppCredentials

    parser = argparse.ArgumentParser(
        prog = "TeamsSSB",
        description="Send out all allocations for secret santa",
    )
    parser.add_argument("--list", action="store_true", help="List available secret santas")
    parser.add_argument("secret_santa_id", default=None, nargs="?")
    args = parser.parse_args()

    if args.list:
        rows = teamsbot.TeamsSecretSantaBase.query.all()
        for row in rows:
            if row.secret_santa:
                print(f"ID: {row.secret_santa_id} - {row.secret_santa.name}")
            else:
                print(f"Team {row.team_id} Not Running")
        sys.exit(0)

    if args.secret_santa_id is None:
        print("A secret santa ID must be provided. Use \"--list\" to show available IDs")
        sys.exit(1)

    # Create teamsbot
    bot = teamsbot.TeamsBot(config.botframework.app_id, config.botframework.app_password)
    ssb = teamsbot.TeamsSecretSantaBase.from_secret_santa_id(args.secret_santa_id)
    ssb.connector_client = ConnectorClient(MicrosoftAppCredentials(bot._app_id, bot._app_password, ssb.tenant), ssb.service_url)

    async def send_all_allocations(bot, ssb):
        creator = ssb.creator
        await ssb.send_all_allocations(creator)
        await bot.message_creator(ssb, "Sent all allocations!")

    asyncio.run(send_all_allocations(bot, ssb))
