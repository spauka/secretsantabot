import configparser
import json
import re
from operator import attrgetter

from flask import Flask, request, render_template, Response
from slackeventsapi import SlackEventAdapter
import tabulate

from sqlalchemy.orm.exc import NoResultFound

import database
import secretsanta
from bot import SlackBot

def send_allocation(slackbot, channel, message, name):
    """
    Send out an allocation to an individual with a given message.
    Args:
     - channel: channel_id for the channel to post to
     - message: the message to accompy the allocation
     - name: name of the person to whom the message will be sent
    """
    slackbot.post_message(channel, message)
    slackbot.post_message(channel, None, attachments=render_template("reveal.txt", name=name))

###
# Flask App
###
app = Flask(__name__)

config = configparser.ConfigParser()
config.read("/home/spauka/secretsanta/secretsanta.cfg")
slack_conf = config['slack']

# Initialize variables in the slack client
SlackBot.client_id = slack_conf["client_id"]
SlackBot.client_secret = slack_conf["client_secret"]
SlackBot.signing_secret = slack_conf["signing_secret"]

@app.teardown_appcontext
def shutdown_session(exception=None):
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

    if payload['type'] == "interactive_message":
        callback_id = payload['callback_id']
        if callback_id == "reveal_ss":
            # Replace button with name of giftee
            action = payload["actions"][0]
            name = action["value"]
            person = find_person(name)
            if person is None:
                return Response("Couldn't find your secret santa. Try ask me again!")
            giftee = ss.has_who(person)

            # Update the persons seen status if necessary
            if not person.seen:
                person.seen = True
                write_people(ss_conf["people_list"], people)

            response = render_template("reveal_done.txt", ss_name=giftee.name)
            return Response(response, mimetype="application/json")
        elif callback_id == "hide_ss":
            response = {"text": "If you ever want to see your secret santa again, just type `who do I have`.",
                        "delete_original": True}
            return Response(json.dumps(response), mimetype="application/json")

# And list valid messages
valid_messages = (
    (re.compile(r"(?:hi|hello) ?(.*)", re.I), "say_hi"),
    (re.compile(r"update people list", re.I), "update_people"),
    (re.compile(r"print everyone ?(with allocations)?", re.I), "print_everyone"),
    (re.compile(r"who do i have", re.I), "print_me"),
    (re.compile(r"who has (.+)", re.I), "who_has"),
    (re.compile(r"who does (.+) have", re.I), "has_who"),
    (re.compile(r"send allocation to (.+)", re.I), "send_allocation_to"),
    (re.compile(r"send admin help to (.+)", re.I), "send_admin_help"),
    (re.compile(r"post welcome message", re.I), "post_welcome_message"),
    (re.compile(r"help", re.I), "return_help"),
)

# Create the event handler
slack_events_adapter = SlackEventAdapter(SlackBot.signing_secret, '/listening', app)

# Reply back to DMs
@slack_events_adapter.on('message')
def respond(event_data):
    # Extract message data
    message = event_data["event"]

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
    message_channel = message["channel"]

    # Respond to DM commands
    if message_type == "im" and message_subtype is None:
        for search, action in valid_messages:
            match = search.fullmatch(message_text)
            if match:
                getattr(team, action)(message, *match.groups())
                break
        else:
            slackbot.post_message(message_channel, "I'm not sure how to respond to that. Type `help` to see what I can do")

if __name__ == "__main__":
    app.env = "development"
    app.run(port=8089, debug=True)
