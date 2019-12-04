"""
Slack chat bot for secret santa
"""
from functools import lru_cache

from slack import WebClient
from flask import render_template
from sqlalchemy import Table, Column, Integer, String, Boolean, DateTime, \
        Sequence, ForeignKey
from sqlalchemy.orm.exc import NoResultFound

from database import Base, db_session
from secretsanta import SecretSanta, Person

class BotDecorators(object):
    @staticmethod
    def ensure_admin(f):
        """
        Decorator that checks whether a user is an admin before allowing
        them to access secret information.
        """
        def ensure_admin_wrapped(self, message, *args):
            # Try to figure out who we are
            person = self.get_person(message["user"])
            if person is None:
                message_channel = message["channel"]
                self.post_message(message_channel, "Oops, I don't know who you are!")
                return
            
            # Run if we are an admin
            if self.check_admin(person):
                return f(self, message, *args)
            
            # Otherwise print an error
            message_channel = message["channel"]
            self.post_message(message_channel, "Ho Ho No")
            return
        
        return ensure_admin_wrapped

    def check_admin(self, p):
        """
        Check if a person is an admin
        """
        # If there is no secret santa, anyone can be an admin
        if self.secret_santa is None:
            return True

        # Otherwise check against the admin list
        return p in self.secret_santa.admins

class Bot(BotDecorators):
    def say_hi(self, message, usr_msg):
        """
        First handler, just says hi
        """
        message_channel = message["channel"]
        message_user = message["user"]

        # Get info about the user
        user = self.get_user_info(message_user)
        
        # Say hi back
        self.post_message(message_channel, f"Hi {user['real_name']}.")

    @BotDecorators.ensure_admin
    def update_people(self, message):
        """
        Update the list of people, filling in Slack_ID's for people whos full names match
        someone already in the list, and adding new people to the list from slacks membership
        list.

        People are marked as non-participants by default.
        """
        message_channel = message["channel"]
        # First get a list of users off slack
        resp = self.client.users_list()
        members = resp["members"]
        for member in members:
            username = member["name"]
            realname = member.get("real_name", "")
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
            try:
                person = Person.query.filter(Person.name == realname).one()
                person.slack_id = userid
                person.email = email
                db_session.commit()
            except NoResultFound:
                person = Person(realname, userid, email)
                db_session.add(person)
                db_session.commit()

        # And update output
        self.post_message(message_channel, "Updated users list")

    @BotDecorators.ensure_admin
    def who_has(self, message, name):
        """
        Print out who has a given person
        """
        message_channel = message["channel"]

        # Look up the person
        person = Person.find_person(name)
        if person is None:
            self.post_message(message_channel, f"Couldn't find {name}")
            return
        if person not in self.secret_santa.participants:
            self.post_message(message_channel, f"{person.name} is not participating in the Secret Santa")
            return
        # Look up the gifter
        gifter = self.secret_santa.who_has(person)

        message = f"{person.name} is getting a gift from {gifter.name}"
        self.post_message(message_channel, message)

    @BotDecorators.ensure_admin
    def has_who(self, message, name):
        """
        Print out who the given person has
        """
        message_channel = message["channel"]

        # Look up the person
        person = Person.find_person(name)
        if person is None:
            self.post_message(message_channel, f"Couldn't find {name}")
            return
        if person not in self.secret_santa.participants:
            self.post_message(message_channel, f"{person.name} is not participating in the Secret Santa")
            return
        giftee = self.secret_santa.has_who(person)

        message = f"{person.name} is giving a gift to {giftee.name}"
        self.post_message(message_channel, message)

    def print_me(self, message):
        """
        Print out who the given person has
        """
        message_channel = message["channel"]
        person = Person.find_person(message['user'])
        if person is None:
            self.post_message(message_channel, f"Couldn't look you up...")
            return
        if not person.participant:
            self.post_message(message_channel, f"I didn't find you in the secret santa...")
            return

        message = "Press the button below to reveal your secret santa: "
        send_allocation(message_channel, message, person.name)

    @BotDecorators.ensure_admin
    def print_everyone(self, message, with_allocations=False):
        """
        Print out a list of everyone, optionally with allocations.
        """
        message_channel = message["channel"]
        headers = ("Name", "Email", "Participating?", "Seen?")
        if with_allocations:
            headers += ("Is Gifting","Getting a gift from")

        # Fill in output
        output = []
        for person in sorted(people, key=attrgetter("normalized_name")):
            info = (person.name, person.email, person.participant, person.seen)
            if with_allocations and person.participant:
                info += (ss.has_who(person).name, ss.who_has(person).name)
            elif with_allocations and not person.participant:
                info += ("", "")
            output.append(info)

        # And reply with the allocations
        self.post_message(message_channel, "People are: ")
        for i in range(ceil(len(output)/25)):
            message = tabulate.tabulate(output[i*25:(i+1)*25], headers)
            self.post_message(message_channel, f"```\n{message}\n```")

    @BotDecorators.ensure_admin
    def send_admin_help(self, message, name):
        """
        Send out the admin help message to a user
        """
        message_channel = message["channel"]

        # Find the person we want to send the message to
        person = Person.find_person(name)
        if person is None:
            self.post_message(message_channel, f"Couldn't find the user {name}...")
            return
        if person.slack_id is None:
            self.post_message(message_channel, f"User {name} is not on slack")
            return
        if self.secret_santa and person not in self.secret_santa.admins:
            self.post_message(message_channel, f"User {name} is not an admin... I won't send them the help!")
            return
        dm_id = self.open_dm(person.slack_id)
        message = render_template("admin_reveal.txt", realname=person.name)
        self.post_message(dm_id, message)
        self.post_message(message_channel, f"Sent admin welcome message to {person.name}")

    @BotDecorators.ensure_admin
    def post_welcome_message(self, message):
        """
        Post a getting started message to general
        """
        message_channel = message["channel"]
        general = self.get_channel_by_name("general")
        if general is None:
            self.post_message(message_channel, "Couldn't find channel")
            return
        self.post_message(general, render_template("general_message.txt", secretsantabot=self.bot_user_id, admin=find_person("Sebastian Pauka").slack_id))
        self.post_message(message_channel, "Welcome message sent!")

    def return_help(self, message):
        """
        Give a usage string for secretsanta bot
        """
        message_channel = message["channel"]
        user = message["user"]
        if self.check_admin(user):
            self.post_message(message_channel, render_template("admin_help.txt"))
        else:
            self.post_message(message_channel, render_template("help.txt"))

class SlackBot(Base, Bot):
    """
    Bot for handling slack interactions
    """
    __tablename__ = "slackbot"
    team_id = Column(String, primary_key=True)
    team_name = Column(String)
    bot_token = Column(String)
    bot_user_id = Column(String)

    name = Column(String)
    friendly_name = Column(String)
    emoji = Column(String)

    # Secret Santa Connection! This is the instance that is currently running in the team
    secret_santa = Column(Integer, ForeignKey(SecretSanta.id))
    
    # Set up global parameters, which will be filled in by the config
    client_id = None
    client_secret = None
    scope = "bot"
    signing_secret = None

    def __init__(self, team_id, team_name, bot_token, bot_user_id):
        super().__init__()

        self.name = "secretsantabot"
        self.friendly_name = "Secret Santa Bot"
        self.emoji = ":robot_face:"

        # Save oauth creds
        self.team_id = team_id
        self.team_name = team_name
        self.bot_token = bot_token
        self.bot_user_id = bot_user_id

        # And make a client
        self.client = WebClient(bot_token)
    
    @classmethod
    def auth(cls, code):
        """
        Authenticate with OAuth and assign correct scopes. Creates a new SlackBot when done.
        Save a dictionary of authed team information in memory on the bot
        object.
        Parameters
        ----------
        code : str
            temporary authorization code sent by Slack to be exchanged for an
            OAuth token
        """
        # First create a new unauthenticated client
        client = WebClient("")
        # After the user has authorized this app for use in their Slack team,
        # Slack returns a temporary authorization code that we'll exchange for
        # an OAuth token using the oauth.access endpoint
        auth_response = client.oauth_access(client_id = cls.client_id,
                client_secret = cls.client_secret,
                code = code)
        
        # To keep track of authorized teams and their associated OAuth tokens,
        # we will save the team ID and bot tokens to the global
        # authed_teams object
        new_self = cls(auth_response["team_id"], auth_response["team_name"],
                auth_response["bot"]["bot_access_token"], auth_response["bot"]["bot_user_id"])
        db_session.add(new_self)
        db_session.commit()

        # And add a new user for the person that added the bot, if it doesn't exist
        if new_self.get_person(auth_response["user_id"]) is None:
            user_info = new_self.get_user_info(auth_response["user_id"])
            new_user = Person(user_info["real_name"], user_info["id"], user_info["profile"]["email"])
            db_session.add(new_user)
            db_session.commit()

        # Return the authentication for storage
        return new_self

    @classmethod
    def from_team(cls, team):
        bot = cls.query.filter(cls.team_id==team).one()
        bot.client = WebClient(bot.bot_token)
        return bot

    @lru_cache()
    def open_dm(self, user):
        """
        Open a direct chat with the given user.
        Note: user should be a valid slack ID
        """
        resp = self.client.api_call("im.open", user=user)
        if not resp["ok"]:
            raise RuntimeError(f"Failed to open dm with {user}")
        return resp["channel"]["id"]

    @lru_cache()
    def get_channel_by_name(self, handle):
        """
        Get the id of the channel with given handle
        """
        resp = self.client.api_call("conversations.list", exclude_archived=True, types="public_channel,private_channel")
        if not resp["ok"]:
            raise RuntimeError(f"Failed to load channels")
        for channel in resp["channels"]:
            if channel["name"] == handle:
                return channel["id"]
        return None

    def get_person(self, slack_id):
        """
        Return the person with the given slack id
        """
        return Person.query.filter(Person.slack_id == slack_id).one_or_none()

    def post_message(self, channel, message, attachments=None):
        """
        Post a message to the given channel
        Args:
            channel: channel id to send the message to
            message: the text of the message
        """
        self.client.chat_postMessage(channel=channel, text=message, attachments=attachments)

    def get_user_info(self, user_id):
        """
        Get information about a user
        Args:
            user_id: User id in the message
        Returns:
            {name: username, real_name: real_name}
        """
        resp = self.client.users_info(user=user_id)
        return resp["user"]


