import sys
import argparse
import traceback
from datetime import datetime
from http import HTTPStatus

from aiohttp import web
from aiohttp.web import StaticDef, Request, Response, json_response
from botbuilder.core import (
        BotFrameworkAdapterSettings,
        TurnContext,
        BotFrameworkAdapter,
)
from botbuilder.core.integration import aiohttp_error_middleware
from botbuilder.schema import Activity, ActivityTypes

import config
from teamsbot import TeamsBot

# Create adapter.
SETTINGS = BotFrameworkAdapterSettings(config.botframework.app_id, config.botframework.app_password)
ADAPTER = BotFrameworkAdapter(SETTINGS)

# Create teamsbot
BOT = TeamsBot(config.botframework.app_id, config.botframework.app_password)

# Catch-all for errors.
async def on_error(context: TurnContext, error: Exception):
    # This check writes out errors to console log .vs. app insights.
    print(f"\n [on_turn_error] unhandled error: {error}", file=sys.stderr)
    traceback.print_exc()

    # Send a message to the user
    await context.send_activity("The bot encountered an error or bug.")

    # Send a trace activity if we're talking to the Bot Framework Emulator
    if context.activity.channel_id == "emulator":
        # Create a trace activity that contains the error object
        trace_activity = Activity(
            label="TurnError",
            name="on_turn_error Trace",
            timestamp=datetime.utcnow(),
            type=ActivityTypes.trace,
            value=f"{error}",
            value_type="https://www.botframework.com/schemas/error",
        )
        # Send a trace activity, which will be displayed in Bot Framework Emulator
        await context.send_activity(trace_activity)
ADAPTER.on_turn_error = on_error

# Listen for incoming requests on /api/messages
async def messages(req: Request) -> Response:
    # Main bot message handler.
    if "application/json" in req.headers["Content-Type"]:
        body = await req.json()
    else:
        return Response(status=HTTPStatus.UNSUPPORTED_MEDIA_TYPE)

    activity = Activity().deserialize(body)
    auth_header = req.headers["Authorization"] if "Authorization" in req.headers else ""

    response = await ADAPTER.process_activity(activity, auth_header, BOT.on_turn)
    if response:
        return json_response(data=response.body, status=response.status)
    return Response(status=HTTPStatus.OK)

async def blank(req: Request) -> Response:
    return Response(status=HTTPStatus.OK, text="")

async def favicon(req: Request) -> Response:
    return web.FileResponse('./images/favicon.ico')


APP = web.Application(middlewares=[aiohttp_error_middleware])
APP.router.add_get("/", blank)
APP.router.add_post("/api/messages", messages)
APP.router.add_get("/favicon.ico", favicon)
APP.add_routes([web.static('/images', './images')])

if __name__ == "__main__":
    # Take path to socket from command line
    parser = argparse.ArgumentParser(description="Secret Santa Bot")
    parser.add_argument('--path')
    args = parser.parse_args()

    try:
        if args.path is None:
            web.run_app(APP, host="localhost", port=config.botframework.port)
        else:
            web.run_app(APP, path=args.path)
    except Exception as error:
        raise error
