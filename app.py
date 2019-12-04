import configparser
import json
import re
from operator import attrgetter
from collections import namedtuple
import html

from flask import Flask, request, render_template, Response
from celery import Celery
from slackeventsapi import SlackEventAdapter

from sqlalchemy.orm.exc import NoResultFound

import database
import secretsanta
from bot import SlackBot


def make_celery(app, backend, broker):
    celery = Celery(
        app.import_name,
        backend=backend,
        broker=broker
    )
    celery.conf.update(app.config)

    class ContextTask(celery.Task):
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)

    celery.Task = ContextTask
    return celery

###
# Flask App
###
app = Flask(__name__)

config = configparser.ConfigParser()
config.read("/home/spauka/secretsanta/secretsanta.cfg")
slack_conf = config['slack']
celery_backend = config['celery']['result_backend']
celery_broker = config['celery']['broker_url']
celery = make_celery(app, celery_backend, celery_broker)

# Initialize variables in the slack client
SlackBot.client_id = slack_conf["client_id"]
SlackBot.client_secret = slack_conf["client_secret"]
SlackBot.signing_secret = slack_conf["signing_secret"]

@app.teardown_appcontext
def shutdown_session(exception=None):
    # Close db connection
    database.db_session.remove()

@app.route("/install", methods=["GET"])
def pre_install():
    """This route renders the installation page with 'Add to Slack' button."""
    client_id = SlackBot.client_id
    scope = SlackBot.scope
    return render_template("install.html", client_id=client_id, scope=scope)


@app.route("/thanks", methods=["GET", "POST"])
def thanks():
    """
    This route is called by Slack after the user installs our app. It will
    exchange the temporary authorization code Slack sends for an OAuth token
    which we'll save on the bot object to use later.
    To let the user know what's happened it will also render a thank you page.
    """
    # Let's grab that temporary authorization code Slack's sent us from
    # the request's parameters.
    code_arg = request.args.get('code')
    # The bot's auth method to handles exchanging the code for an OAuth token
    new_bot = SlackBot.auth(code_arg)

    return render_template("thanks.html")

@app.route("/messages", methods=["GET", "POST"])
def handle_message():
    """
    This route is used to handle interactive messages
    """
    payload = json.loads(request.form['payload'])
    
    # Assign message to a team
    try:
        message_team = payload["team"]["id"]
        team = SlackBot.from_team(message_team)
    except NoResultFound:
        # Message not associated with a team, just pass it back...
        return

    if payload['type'] == "interactive_message":
        callback_id = payload['callback_id']
        if callback_id == "reveal_ss":
            # Replace button with name of giftee
            action = payload["actions"][0]
            person = team.get_person(payload["user"]["id"])
            if person is None:
                return Response("Couldn't find your secret santa. Try ask me again!")
            try:
                giftee = team.secret_santa.has_who(person)
                team.secret_santa.update_seen(person)

                response = render_template("reveal_done.txt", ss_name=giftee.name)
                return Response(response, mimetype="application/json")
            except ValueError:
                return Response("Couldn't find your secret santa. Try ask me again!")
        elif callback_id == "hide_ss":
            response = {"text": "If you ever want to see your secret santa again, just type `who do I have`.",
                        "delete_original": True}
            return Response(json.dumps(response), mimetype="application/json")

valid_messages = (
    (re.compile(r"(?:hi|hello) ?(.*)", re.I), "say_hi"),
    (re.compile(r"start a new secret santa: ?(.+)", re.I), "start_secret_santa"),
    (re.compile(r"add person: ?([^<>]+) <?<[^|]*\|?([^>]+)>>?", re.I), "add_person"),
    (re.compile(r"add person: ?([^<>]+)", re.I), "add_person"),
    (re.compile(r"add admin: ?(.+)", re.I), "add_admin"),
    (re.compile(r"remove person: ?(.+)", re.I), "remove_person"),
    (re.compile(r"print everyone ?(with allocations)?", re.I), "print_everyone"),
    (re.compile(r"do allocations", re.I), "do_allocations"),
    (re.compile(r"who do i have", re.I), "print_me"),
    (re.compile(r"who has ([^?]+)\??", re.I), "who_has"),
    (re.compile(r"who does (.+) have\??", re.I), "has_who"),
    (re.compile(r"send all allocations", re.I), "send_all_allocations"),
    (re.compile(r"send allocation to (.+)", re.I), "send_allocation_to"),
    (re.compile(r"send admin help to (.+)", re.I), "send_admin_help"),
    (re.compile(r"post welcome message", re.I), "post_welcome_message"),
    (re.compile(r"help", re.I), "return_help"),
)

@celery.task(serializer='json')
def handle_messages(message):
    """
    Handle messages in the background. This is to allow Flask to return a success to Slack immediately.
    """
    # Assign message to a team
    try:
        message_team = message.get("team")
        team = SlackBot.from_team(message_team)
    except NoResultFound:
        # Message not associated with a team, just pass it back...
        return
 
    # Extract message data
    message_type = message.get("channel_type", None)
    message_subtype = message.get("subtype", None)
    message_text = message.get("text", "")
    message_text = html.unescape(message_text)
    message_channel = message["channel"]

    # Respond to DM commands
    if message_type == "im" and message_subtype is None:
        for search, action in valid_messages:
            match = search.fullmatch(message_text)
            if match:
                getattr(team, action)(message, *match.groups())
                break
        else:
            print(message_text)
            team.post_message(message_channel, "I'm not sure how to respond to that. Type `help` to see what I can do")


# Create the event handler
slack_events_adapter = SlackEventAdapter(SlackBot.signing_secret, '/listening', app)

# Reply back to DMs
@slack_events_adapter.on('message')
def respond(event_data):
    # Retrieve message and pass to background task
    message = event_data["event"]
    handle_messages.delay(message).forget()

if __name__ == "__main__":
    app.env = "development"
    app.run(port=8089, debug=True)
