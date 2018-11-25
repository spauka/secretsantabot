import configparser
import json
import re
from operator import attrgetter

from flask import Flask, request, render_template, Response
from slackeventsapi import SlackEventAdapter
import tabulate

import secretsanta
import bot

###
# Convenience Functions
###
def read_people(fname, has_headers=True):
    """
    Read in the list of people from the list
    """
    people = []
    with open(fname, "r") as fhandle:
        if has_headers:
            fhandle.readline()
        for line in fhandle:
            people.append(secretsanta.Person.from_str(line.strip()))
    return people

def write_people(fname, people):
    """
    Write out a person list
    """
    with open(fname, "w") as fhandle:
        fhandle.write("Name, Slack ID, email, force_email\n")
        fhandle.write("\n".join(str(person) for person in people))

def refresh_secretsanta(people, seed):
    active_people = [person for person in people if person.participant]
    ss = secretsanta.SecretSanta(active_people, seed=seed)
    return ss

def find_person(person_str):
    """
    Match a person to a person_str which may be a slack user id,
    or a full name
    """
    # Match by Slack ID
    slack_id = re.fullmatch("<@(\w+)>", person_str)
    if slack_id:
        slack_id = slack_id.groups()[0]
        for person in people:
            if person.slack_id == slack_id:
                return person
    # Match by name
    for person in people:
        if person.name == person_str:
            return person
        if person.normalized_name == person_str:
            return person
    return None

def check_admin(userid):
    """
    Check if a given user id is an admin
    """
    admins = ss_conf["admins"].strip().split()
    return userid in admins

def ensure_admin(f):
    """
    Decorator that checks whether a user is an admin before allowing
    them access to secret information.
    """
    def ensure_admin_wrapped(message, *args):
        message_channel = message["channel"]
        user = message.get("user", None)
        if user is None:
            slackbot.post_message(message_channel, "Oops, couldn't check user...")
        if check_admin(user):
            f(message, *args)
        else:
            slackbot.post_message(message_channel, "Ah ah ah, you didn't say the magic word...")

    return ensure_admin_wrapped

###
# Flask App
###
app = Flask(__name__)

config = configparser.ConfigParser()
config.read("secretsanta.cfg")
ss_conf = config['secretsanta']
slack_conf = config['slack']

# Read in the list of people, and create the secret santa
with app.app_context():
    people = read_people(ss_conf['people_list'],
                        ss_conf.getboolean('has_headers'))
    ss = refresh_secretsanta(people, ss_conf["seed"])

    # Create the slack bot
    slackbot = bot.Bot(slack_conf["client_id"], slack_conf["client_secret"], slack_conf["signing_secret"])
    # Check if we've already authenticated
    active_team = slack_conf.get("active_team", None)
    if active_team is not None:
        slackbot.client = bot.SlackClient(config[active_team]["bot_token"])

@app.route("/install", methods=["GET"])
def pre_install():
    """This route renders the installation page with 'Add to Slack' button."""
    # Since we've set the client ID and scope on our Bot object, we can change
    # them more easily while we're developing our app.
    client_id = slackbot.client_id
    scope = slackbot.scope
    # Our template is using the Jinja templating language to dynamically pass
    # our client id and scope
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
    authentication = slackbot.auth(code_arg)

    # Save the authentication token into the config
    config[authentication["team_id"]] = authentication
    with open("secretsanta.cfg", "w") as configfile:
        config.write(configfile)

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
            channel = payload["channel"]["id"]
            ts = payload["message_ts"]
            ss_name = action["value"]
            message = f"You're secret santa is: {ss_name}"

            response = {"text": message,
                        "delete_original": False}
            return Response(json.dumps(response), mimetype="application/json")

# Create the event handler
slack_events_adapter = SlackEventAdapter(slackbot.signing_secret, '/listening', app)

def say_hi(message, usr_msg):
    """
    First handler, just says hi
    """
    message_channel = message["channel"]
    message_user = message["user"]

    # Get info about the user
    resp = slackbot.client.api_call("users.info", user=message_user)
    username = resp["user"]["name"]
    realname = resp["user"]["real_name"]

    slackbot.post_message(message_channel, f"Hi {realname}.")

@ensure_admin
def update_people(message):
    """
    Update the list of people, filling in Slack_ID's for people whos full names match
    someone already in the list, and adding new people to the list from slacks membership
    list.

    People are marked as non-participants by default.
    """
    message_channel = message["channel"]
    # First get a list of users off slack
    resp = slackbot.client.api_call("users.list")
    members = resp["members"]
    for member in members:
        username = member["name"]
        realname = member["real_name"]
        userid = member["id"]
        email = member["profile"].get("email", "None")
        if member["is_bot"] or userid == "USLACKBOT":
            print(f"Skipping Bot: {username}")
            continue
        if member["deleted"]:
            print(f"Skipping deleted user: {username}")
            continue

        # Check whether the user is already in the secret santa list
        # and update their slack id and email if they are.
        for person in people:
            if realname == person.name:
                person.slack_id = userid
                person.email = email
                break
        else:
            people.append(secretsanta.Person(realname, userid, email))

    # And rewrite the list
    write_people(ss_conf["people_list"], people)
    with app.app_context():
        ss = refresh_secretsanta(people, ss_conf["seed"])

    # And update output
    slackbot.post_message(message_channel, "Updated users list")

@ensure_admin
def who_has(message, user):
    """
    Print out who has a given person
    """
    message_channel = message["channel"]

    # Look up the person
    person = find_person(user)
    if person is None:
        slackbot.post_message(message_channel, f"Couldn't find {user}")
        return
    if not person.participant:
        slackbot.post_message(message_channel, f"{person.name} is not participating in the Secret Santa")
        return
    # Look up the gifter
    gifter = ss.who_has(person)

    message = f"{person.name} is getting a gift from {gifter.name}"
    slackbot.post_message(message_channel, message)

@ensure_admin
def has_who(message, user):
    """
    Print out who the given person has
    """
    message_channel = message["channel"]

    # Look up the person
    person = find_person(user)
    if person is None:
        slackbot.post_message(message_channel, f"Couldn't find {user}")
        return
    if not person.participant:
        slackbot.post_message(message_channel, f"{person.name} is not participating in the Secret Santa")
        return
    giftee = ss.has_who(person)

    message = f"{person.name} is giving a gift to {giftee.name}"
    slackbot.post_message(message_channel, message)

def print_me(message):
    """
    Print out who the given person has
    """
    message_channel = message["channel"]
    person = find_person(f"<@{message['user']}>")
    if person is None:
        slackbot.post_message(message_channel, f"Couldn't look you up...")
        return
    if not person.participant:
        slackbot.post_message(message_channel, f"{person.name} is not participating in the Secret Santa")
        return
    giftee = ss.has_who(person)

    message = f"You're giving a gift to: {giftee.name}"
    slackbot.post_message(message_channel, message)

@ensure_admin
def print_everyone(message, with_allocations=False):
    """
    Print out a list of everyone, optionally with allocations.
    """
    message_channel = message["channel"]
    headers = ("Name", "Email", "Participating?")
    if with_allocations:
        headers += ("Is Gifting","Getting a gift from")

    # Fill in output
    output = []
    for person in sorted(people, key=attrgetter("normalized_name")):
        info = (person.name, person.email, person.participant)
        if with_allocations and person.participant:
            info += (ss.has_who(person).name, ss.who_has(person).name)
        elif with_allocations and not person.participant:
            info += ("", "")
        output.append(info)

    # And reply with the allocations
    message = "People are: \n```"
    message += tabulate.tabulate(output, headers)
    message += "```"
    slackbot.post_message(message_channel, message)

@ensure_admin
def send_allocations(message):
    """
    Send out allocations to everyone
    """
    message_channel = message["channel"]

    # Loop over the list of participants
    for person in ss.secret_santa:
        # Figure out who they have
        realname = person.name
        ss_name = ss.has_who(person).name
        # Check if they have a slack ID
        if person.slack_id is not None and person.slack_id != "None":
            dm_id = slackbot.open_dm(person.slack_id)
            message = render_template("message.txt", realname=realname)
            slackbot.post_message(dm_id, message)
            slackbot.post_message(dm_id, None, attachments=render_template("reveal.txt", ss_name=ss_name))
        else:
            # We have to send out an email instead
            pass

    # Send success
    slackbot.post_message(message_channel, "Successfully sent out allocations")

def return_help(message):
    """
    Give a usage string for secretsanta bot
    """
    message_channel = message["channel"]
    user = message["user"]
    if check_admin(user):
        slackbot.post_message(message_channel, render_template("admin_help.txt"))
    else:
        slackbot.post_message(message_channel, render_template("help.txt"))

# And list valid messages
valid_messages = (
    (re.compile(r"(?:hi|hello) ?(.*)", re.I), say_hi),
    (re.compile(r"update people list", re.I), update_people),
    (re.compile(r"print everyone ?(with allocations)?", re.I), print_everyone),
    (re.compile(r"who do i have", re.I), print_me),
    (re.compile(r"who has (.+)", re.I), who_has),
    (re.compile(r"who does (.+) have", re.I), has_who),
    (re.compile(r"send out allocations", re.I), send_allocations),
    (re.compile(r"help", re.I), return_help),
)

# Reply back to DMs
@slack_events_adapter.on('message')
def respond(event_data):
    # Extract message data
    message = event_data["event"]
    message_type = message.get("channel_type", None)
    message_subtype = message.get("subtype", None)
    message_text = message.get("text", "")
    message_channel = message["channel"]

    # Respond to DM commands
    if message_type == "im" and message_subtype is None:
        for search, action in valid_messages:
            match = search.fullmatch(message_text)
            if match:
                action(message, *match.groups())
                break
        else:
            slackbot.post_message(message_channel, "I'm not sure how to respond to that. Type `help` to see what I can do")

if __name__ == "__main__":
    app.env = "development"
    app.run(port=8888, debug=True)
