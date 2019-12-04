"""
Slack chat bot for secret santa
"""
from functools import lru_cache
from operator import attrgetter
from math import ceil
import tabulate

from slack import WebClient
from flask import render_template
from sqlalchemy import Table, Column, Integer, String, Boolean, DateTime, \
        Sequence, ForeignKey
from sqlalchemy.orm import relationship, backref
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
                # We might still be found by slack id
                person = Person.query.filter(Person.slack_id == message["user"]).first()
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

    def send_allocation(self, channel, message, name):
        """
        Send out an allocation to an individual with a given message.
        Args:
         - channel: channel_id for the channel to post to
         - message: the message to accompy the allocation
         - name: name of the person to whom the message will be sent
        """
        self.post_message(channel, message)
        self.post_message(channel, None, attachments=render_template("reveal.txt", name=name))

    def send_allocation_to(self, message, name):
        """
        Send out an allocation to an individual.
        """
        message_channel = message["channel"]
        message_user = message["user"]

        # Find the person to send the allocation to
        participant = self.get_participant_by_name(name)
        if participant is None:
            self.post_message(message_channel, f"Couldn't find {name}")

        # Send their allocation
        message = "Press the button below to reveal your secret santa: "
        dm = self.open_dm(participant.participant.slack_id)
        self.send_allocation(dm, message, participant.participant.name)
        self.post_message(message_channel, f"Sent allocation to {name}.")

    def send_all_allocations(self, message):
        """
        Send out all allocations
        """
        message_channel = message["channel"]
        message_user = message["user"]

        # Check that a drawing has been done
        if self.secret_santa is None:
            self.post_message(message_channel, f"No active secret santa")
        if self.secret_santa.seed is None:
            self.post_message(message_channel, f"Drawing hasn't been made")

        # Loop through participants and send allocations
        for participant in self.secret_santa.participants:
            # Check that the person has been drawn
            if participant.ordering is None:
                print(f"Skipping {participant}")
                self.post_message("Skipping {participant}")
                continue
            # Send their allocation if they have a Slack ID
            if participant.participant.slack_id is not None:
                dm = self.open_dm(participant.participant.slack_id)
                message = "Your secret santa has been drawn. Press the button below to reveal your secret santa: "
                self.send_allocation(dm, message, participant.participant.name)
                print(f"Sent allocation to {participant.participant.name}.")
            else:
                self.post_message(message_channel, f"Send allocation to {participant.participant.name} manually.")
                print(f"Send allocation to {participant.participant.name} manually.")
        self.post_message(message_channel, f"Done sending allocations!")

    @BotDecorators.ensure_admin
    def start_secret_santa(self, message, name):
        """
        Start up a secret santa
        """
        message_channel = message["channel"]
        message_user = message["user"]

        # Get a list of people in the team
        people = []
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

            person = Person(realname, userid, email)
            db_session.add(person)
            db_session.commit()
            people.append(person)

        # Create a new secret santa
        s = SecretSanta(name, people)
        db_session.add(s)

        # Assign it to our team
        self.secret_santa = s

        # And make the user an admin
        user = self.get_person(message_user)
        s.admins.append(user)

        # Done
        db_session.commit()
        self.post_message(message_channel, f"Started a new secret santa with name: {name}")

    @BotDecorators.ensure_admin
    def add_person(self, message, name, email=None):
        """
        Add a person to the secret santa
        """
        message_channel = message["channel"]
        
        # Make a new person
        person = Person(name, None, email, email is not None)
        db_session.add(person)
        db_session.commit()

        # Add them to the secret santa
        self.secret_santa.add_participant(person)

        # Post status
        if email is not None:
            message = f"Added person {name} with email {email} to the secret santa"
        else:
            message = (f"Added person {name} to the secret santa. Note: This person has no slack or email."
                        "Make sure to tell them their assignment in person.")
        self.post_message(message_channel, message)

    @BotDecorators.ensure_admin
    def add_admin(self, message, name):
        """
        Print out who has a given person
        """
        message_channel = message["channel"]

        # Look up the person
        person = self.get_person_by_name(name)
        if person is None:
            self.post_message(message_channel, f"Couldn't find {name}")
            return
        # Add the person to the admin list
        self.secret_santa.admins.append(person)
        db_session.commit()

        # Return status
        message = f"Made {person.name} an admin"
        self.post_message(message_channel, message)

    @BotDecorators.ensure_admin
    def remove_person(self, message, name):
        """
        Remove a person from the secret santa
        """
        message_channel = message["channel"]
        
        # Find a person and remove them
        person = self.get_participant_by_name(name)
        if person is None:
            self.post_message(message_channel, f"Couldn't find {name}")
        self.secret_santa.participants.remove(person)
        db_session.commit()

        message = f"Removed {name} from the secret santa."
        self.post_message(message_channel, message)

    @BotDecorators.ensure_admin
    def do_allocations(self, message):
        """
        Perform allocations on all participants
        """
        message_channel = message["channel"]

        # Do allocations
        try:
            self.secret_santa.generate_ordering()
        except RuntimeError:
            self.post_message(message_channel, "No can do! The orderings have already been done!")
            return
        self.post_message(message_channel, "Ordering done!")


    @BotDecorators.ensure_admin
    def who_has(self, message, name):
        """
        Print out who has a given person
        """
        message_channel = message["channel"]

        # Look up the person
        person = self.get_person_by_name(name)
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
        person = self.get_person_by_name(name)
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
        person = self.get_person(message['user'])
        if person is None:
            self.post_message(message_channel, f"Couldn't look you up...")
            return

        message = "Press the button below to reveal your secret santa: "
        self.send_allocation(message_channel, message, person.name)

    @BotDecorators.ensure_admin
    def print_everyone(self, message, with_allocations=False):
        """
        Print out a list of everyone, optionally with allocations.
        """
        message_channel = message["channel"]
        headers = ("Name", "Email", "Seen?")
        if with_allocations:
            headers += ("Is Gifting","Getting a gift from")

        # Fill in output
        output = []
        people = self.secret_santa.participants
        for participant in sorted(people, key=attrgetter("participant.normalized_name")):
            person = participant.participant
            info = (person.name, person.email, participant.seen)
            if with_allocations:
                if participant.ordering is None:
                    info += ("Not Allocated", "Not Allocated")
                else:
                    info += (self.secret_santa.has_who(person).name, self.secret_santa.who_has(person).name)
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
        person = self.get_person_by_name(name)
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
        self.post_message(general, render_template("general_message.txt", 
            secretsantabot=self.bot_user_id, 
            admin=self.get_person_by_name("Sebastian Pauka").slack_id))
        self.post_message(message_channel, "Welcome message sent!")

    def return_help(self, message):
        """
        Give a usage string for secretsanta bot
        """
        message_channel = message["channel"]
        user = self.get_person(message["user"])
        if user is not None and self.check_admin(user):
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
    secret_santa_id = Column(Integer, ForeignKey(SecretSanta.id))
    secret_santa = relationship("SecretSanta", backref=backref("slackbot", uselist=False))
    
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
        resp = self.client.im_open(user=user)
        if not resp["ok"]:
            raise RuntimeError(f"Failed to open dm with {user}")
        return resp["channel"]["id"]

    @lru_cache()
    def get_channel_by_name(self, handle):
        """
        Get the id of the channel with given handle
        """
        resp = self.client.conversations_list(exclude_archived=1, types="public_channel,private_channel")
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
        for participant in self.secret_santa.participants:
            if participant.participant.slack_id == slack_id:
                return participant.participant
        for person in self.secret_santa.admins:
            if person.slack_id == slack_id:
                return person
        return None

    def get_participant_by_name(self, name):
        """
        Return the person with the given slack id
        """
        for participant in self.secret_santa.participants:
            if participant.participant.normalized_name == Person.normalize_name(name):
                return participant
    
    def get_person_by_name(self, name):
        """
        Return the person with the given slack id
        """
        for participant in self.secret_santa.participants:
            if participant.participant.normalized_name == Person.normalize_name(name):
                return participant.participant
        for person in self.secret_santa.admins:
            if person.normalized_name == Person.normalize_name(name):
                return person
        return None

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


