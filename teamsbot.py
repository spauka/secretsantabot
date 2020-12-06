import re
from functools import lru_cache
import json
import traceback
import datetime
import asyncio

import jinja2
from sqlalchemy import Table, Column, Integer, String, Boolean, DateTime, \
        Sequence, ForeignKey
from sqlalchemy.orm import relationship, backref
from sqlalchemy.orm.exc import NoResultFound

from botbuilder.core import CardFactory, TurnContext, MessageFactory, InvokeResponse
from botbuilder.core.teams import TeamsActivityHandler, TeamsInfo, teams_get_channel_id
from botbuilder.schema import Activity, CardAction, ThumbnailCard, CardImage, Mention, ConversationParameters
from botbuilder.schema.teams import TeamInfo, TeamsChannelAccount
from botbuilder.schema._connector_client_enums import ActionTypes
from botframework.connector.aio import ConnectorClient
from botframework.connector.auth import MicrosoftAppCredentials

from database import Base, db_session
from secretsanta import SecretSanta, Person
from bot import Bot

valid_messages = (
    (re.compile(r"(?:hi|hello) ?(.*)", re.I), "say_hi"),
    (re.compile(r"start a new secret santa: ?(.+)", re.I), "start_secret_santa"),
    (re.compile(r"add person: ?([^<>]+) <?<[^|]*\|?([^>]+)>>?", re.I), "add_person"),
    (re.compile(r"add person: ?([^<>]+)", re.I), "add_person"),
    (re.compile(r"add admin: ?(.+)", re.I), "add_admin"),
    (re.compile(r"remove person: ?(.+)", re.I), "remove_person"),
    (re.compile(r"print everyone ?(with allocations)?", re.I), "print_everyone"),
    (re.compile(r"do allocations ?(redo)?", re.I), "do_allocations"),
    (re.compile(r"who do i have", re.I), "print_me"),
    (re.compile(r"who has ([^?]+)\??", re.I), "who_has"),
    (re.compile(r"who does (.+) have\??", re.I), "has_who"),
    (re.compile(r"send all allocations", re.I), "send_all_allocations"),
    (re.compile(r"send allocation to (.+)", re.I), "send_allocation_to"),
    (re.compile(r"send admin help to (.+)", re.I), "send_admin_help"),
    (re.compile(r"post welcome message", re.I), "post_welcome_message"),
    (re.compile(r"help", re.I), "return_help"),
)

class TeamsBot(TeamsActivityHandler):
    """
    Base query handler for teams. This redirects the messages to an instance of a TeamsSecretSantaBase
    """
    def __init__(self, app_id, app_password):
        # Save bot info
        self._app_id = app_id
        self._app_password = app_password

    async def fill_members(self, turn_context: TurnContext, teams_ssb: "TeamsSecretSantaBase"):
        """
        Get a list of team members on installation.
        """
        paged_members: List[TeamsChannelAccount] = []
        continuation_token = None

        while True:
            current_page = await TeamsInfo.get_paged_members(
                turn_context, continuation_token, 100
            )
            continuation_token = current_page.continuation_token
            paged_members.extend(current_page.members)

            if continuation_token is None:
                break

        # Now that we've got a list of all members, create a person for each one
        for person in paged_members:
            if person.user_role == "bot":
                # Don't bother adding bots
                continue
            print(f"Adding person {person}.")
            new_person = Person(person.name, "teams", person.id, person.email)
            db_session.add(new_person)
            teams_ssb.team_members.append(new_person)
        db_session.commit()

    async def on_installation_update_add(self, turn_context: TurnContext):
        """
        This event fires when the bot is installed in a team.
        Use this to create the team instance
        """
        # The source must be msteams
        if turn_context.activity.channel_id != "msteams":
            return

        # Retrieve the team properties
        # Channel properties
        service_url = turn_context.activity.service_url
        tenant = turn_context.activity.channel_data["tenant"]["id"]
        team_id = turn_context.activity.channel_data["team"]["id"]
        channel_id = teams_get_channel_id(turn_context.activity)

        # Check if the secret santa instance already exists
        try:
            team = TeamsSecretSantaBase.from_tenant(tenant, turn_context)
            # This tenant already exists. For now, let's just fail out
            raise RuntimeError(f"SecretSantaBot already installed in tenant {tenant}")
        except NoResultFound:
            conversation_reference = turn_context.get_conversation_reference(turn_context.activity)
            team = TeamsSecretSantaBase(service_url, team_id, tenant, channel_id, json.dumps(conversation_reference.serialize()), turn_context)
            db_session.add(team)
            db_session.commit()

            # Add all users to the secret santa
            await self.fill_members(turn_context, team)

            # Assign to creator
            team.creator = team.get_person(turn_context.activity.from_property.id)
            print(f"Creator set to {team.creator}")
            db_session.commit()

    async def on_installation_update_remove(self, turn_context: TurnContext):
        """
        This event fires when the bot is installed in a team.
        Use this to remove the team instance.
        """
        # The source must be msteams
        if turn_context.activity.channel_id != "msteams":
            return

        # Retrieve the team properties
        TeamsChannelAccount: creator = None
        creator = await TeamsInfo.get_member(turn_context, turn_context.activity.from_property.id)
        # Channel properties
        service_url = turn_context.activity.service_url
        tenant = turn_context.activity.channel_data["tenant"]["id"]
        team_id = turn_context.activity.channel_data["team"]["id"]
        channel_id = turn_context.activity.channel_data["channel"]["id"]

        # Delete the team
        try:
            team = TeamsSecretSantaBase.from_tenant(tenant, turn_context)
            db_session.delete(team)
            db_session.commit()
        except NoResultFound:
            pass

    async def post_exception(self, team, tb):
        """
        Post an exception to the owner of the secret santa
        """
        if not isinstance(team, TeamsSecretSantaBase):
            raise TypeError(f"Unable to forward exception {e} to unknown team {team}.")

        if team.creator is None:
            raise ValueError(f"Creator is None. Can't forward exception.")

        conversation = await team.open_dm(team.creator)

        head = f"<p>[on_turn_error] unhandled error:</p>"
        message = "\n".join(tb.format())
        message = f"<pre>{jinja2.escape(message)}</pre>"

        await team.post_dm(conversation, f"{head}{message}", m_type="xml")

    async def message_creator(self, team, message, m_type="xml"):
        """
        Message the owner of the team a message
        """
        if not isinstance(team, TeamsSecretSantaBase):
            raise TypeError(f"Unable to forward message {message} to unknown team {team}.")
        if team.creator is None:
            raise ValueError(f"Creator is None. Can't forward exception.")

        conversation = await team.open_dm(team.creator)
        await team.post_dm(conversation, f"{message}", m_type=m_type)

    async def on_message_activity(self, turn_context: TurnContext):
        # Attach the message to a tenant
        tenant = turn_context.activity.channel_data["tenant"]["id"]
        conversation_type = turn_context.activity.conversation.conversation_type
        message_text = turn_context.activity.text.strip()
        person = turn_context.activity.from_property

        # We only handle personal messages - team messages do nothing
        if conversation_type != "personal":
            return

        try:
            # Initialize team
            team = TeamsSecretSantaBase.from_tenant(tenant, turn_context)
            team.connector_client = ConnectorClient(MicrosoftAppCredentials(self._app_id, self._app_password, tenant), team.service_url)

            person = team.get_person(person.id)
            if person is None:
                asyncio.create_task(self.message_creator(f"Trying to message unknown person {turn_context.activity.from_property.name}"))
                return

            print(f"{datetime.datetime.now().isoformat()}: Handling message from {person.name}")

            for search, action in valid_messages:
                match = search.fullmatch(message_text)
                if match:
                    return await getattr(team, action)(person, *match.groups())
            else:
                print(f"Unknown message: {message_text}.")
                return await turn_context.send_activity(
                    MessageFactory.text(f"I'm not sure how to respond to that. Type `help` to see what I can do")
                )

        except NoResultFound:
            return await turn_context.send_activity(
                MessageFactory.text(f"Could not find an installed tenant ({tenant})! Make sure you install SecretSantaBot into a team.")
            )
        except Exception as e:
            if team is not None:
                tb = traceback.TracebackException.from_exception(e, limit=15)
                asyncio.create_task(self.post_exception(team, tb))
            else:
                raise

    async def on_teams_card_action_invoke(self, turn_context: TurnContext):
        """
        Invoked when the user presses a button on the card
        """
        # Attach the message to a tenant
        tenant = turn_context.activity.channel_data["tenant"]["id"]
        conversation_type = turn_context.activity.conversation.conversation_type
        person = turn_context.activity.from_property

        # We only handle personal messages - team messages do nothing
        if conversation_type != "personal":
            # Don't do anything
            return await turn_context.update_activity(turn_context.activity)

        try:
            # Initialize team
            team = TeamsSecretSantaBase.from_tenant(tenant, turn_context)
            person = team.get_person(person.id)
            if person is None:
                asyncio.create_task(self.message_creator(f"Trying to message unknown person {turn_context.activity.from_property.name}"))
                return

            # Perform the correct update action
            if turn_context.activity.value is not None:
                if turn_context.activity.value.get("action", None) == "reveal":
                    return await team.reveal_allocation_card(person, turn_context.activity.reply_to_id)
                if turn_context.activity.value.get("action", None) == "hide":
                    return await turn_context.delete_activity(turn_context.activity.reply_to_id)

            # If we don't know what it is, just delete it
            return await turn_context.delete_activity(turn_context.activity.reply_to_id)
        except NoResultFound:
            message = MessageFactory.text(f"Unable to associate action with an installed tenant ({tenant})."
                                           "The SecretSantaBot must not have been installed correctly.")
            message.id = turn_context.activity.reply_to_id
            return await turn_context.update_activity(message)


class TeamsSecretSantaBase(Base, Bot):
    """
    Bot for handling a given team in teams
    """
    __tablename__ = "teamsbot"
    service_url = Column(String)
    team_id = Column(String, primary_key=True)
    tenant = Column(String)
    channel = Column(String)
    conversation_reference = Column(String)
    creator_id = Column(Integer, ForeignKey("person.id"))
    creator = relationship(Person, foreign_keys=[creator_id])
    team_members = relationship(Person, back_populates="team", foreign_keys=[Person.team_id])

    name = Column(String)
    friendly_name = Column(String)
    emoji = Column(String)

    # Secret Santa Connection! This is the instance that is currently running in the team
    secret_santa_id = Column(Integer, ForeignKey(SecretSanta.id))
    secret_santa = relationship("SecretSanta", backref=backref("slackbot", uselist=False))

    def __init__(self, service_url, team_id, tenant, channel, conversation_reference, turn_context):
        super().__init__()

        self.name = "secretsantabot"
        self.friendly_name = "Secret Santa Bot"
        self.emoji = ":robot_face:"

        # Save teams instance
        self.service_url = service_url
        self.team_id = team_id
        self.tenant = tenant
        self.channel = channel
        self.conversation_reference = conversation_reference
        self.team_members = []

        # Save reply path
        self.turn_context = turn_context

    @classmethod
    def from_tenant(cls, tenant, turn_context):
        bot = cls.query.filter(cls.tenant==tenant).one()
        bot.turn_context = turn_context
        bot.connector_client = None
        return bot

    async def send_allocation_card(self, person):
        """
        Send an allocation card to a specific person
        """
        # Construct the thumbnail card
        image = CardImage(url="https://secretsanta.spauka.se/images/210274.png", alt="Image of Christmas Present")
        action = CardAction(type="invoke", title="Reveal", value={"action": "reveal"})
        card = ThumbnailCard(
            title="Your Secret Santa",
            subtitle="Click on the button below to reveal your secret santa!",
            text="Gifts will be exchanged at Lunch at the Christmas Party on the 18th of December. If you run into any issues let Sebastian Pauka or Alexis George know :D.",
            images=[image],
            buttons=[action]
        )
        card_to_send = CardFactory.thumbnail_card(card)
        message = MessageFactory.attachment(card_to_send)

        # Send the card to the person
        if person.chat_id == self.turn_context.activity.from_property.id:
            # We can just use the reply. This is faster than setting up a DM
            return await self.post_reply(message)
        else:
            dm = await self.open_dm(person)
            return await self.post_dm(dm, message)

    async def reveal_allocation_card(self, person, reply_id):
        """
        Reveal the allocation card of a specific person
        """
        try:
            giftee = self.secret_santa.has_who(person)
            self.secret_santa.update_seen(person)
            print(f"{datetime.datetime.now().isoformat()}: Revealed allocation for {person.name}")
            subtitle = f"Your secret santa is: {giftee.name}"
            message = "To hide this message, press \"Hide\""
            actions = [CardAction(type="invoke", title="Hide", value={"action": "hide"})]
        except ValueError:
            subtitle = f"Oops, you don't seem to be in this secret santa."
            message = "If you think this is a mistake, talk to Sebastian Pauka or Alexis George!"
            actions = []

        # Construct the thumbnail card
        image = CardImage(url="https://secretsanta.spauka.se/images/210274.png", alt="Image of Christmas Present")
        card = ThumbnailCard(
            title="Your Secret Santa",
            subtitle=subtitle,
            text=message,
            images=[image],
            buttons=actions
        )
        card_to_send = CardFactory.thumbnail_card(card)
        message = MessageFactory.attachment(card_to_send)
        message.id = reply_id
        return await self.turn_context.update_activity(message)

    async def open_dm(self, person):
        """
        Open a direct chat with the given user.
        """
        open_connections = getattr(self, "open_connections", {})
        if person in open_connections:
            return open_connections[person]
        if not isinstance(person, Person):
            raise TypeError(f"person should be a Person. Got: {person}")

        teams_ref = TeamsChannelAccount(id=person.chat_id)
        conversation_parameters = ConversationParameters(
            is_group=False,
            bot=self.turn_context.activity.recipient,
            members=[teams_ref],
            tenant_id=self.tenant
        )

        # Create the conversation
        conversation = await self.connector_client.conversations.create_conversation(conversation_parameters)
        print(f"Conversation: {conversation}")
        open_connections[person] = conversation
        return conversation

    async def post_to_general(self, message, m_type="xml"):
        """
        Get a conversation handle to general.
        """
        message = MessageFactory.text(message)
        message.text_format = m_type
        conversation_parameters = ConversationParameters(
            is_group=True,
            channel_data={"channel": {"id": self.channel}},
            activity=message
        )

        # Create the conversation
        conversation = await self.connector_client.conversations.create_conversation(conversation_parameters)
        print(f"Conversation to general: {conversation}")
        return conversation

    def get_person(self, teams_id):
        """
        Return the person with the given teams id
        """
        for person in self.team_members:
            if person.chat_id == teams_id:
                return person
        return None

    def get_participant_by_name(self, name):
        """
        Return the participant with the given name
        """
        if self.secret_santa is None:
            return None
        for participant in self.secret_santa.participants:
            if participant.participant.normalized_name == Person.normalize_name(name):
                return participant
        return None

    def get_person_by_name(self, name):
        """
        Return the person with the given name
        """
        for participant in self.secret_santa.participants:
            if participant.participant.normalized_name == Person.normalize_name(name):
                return participant.participant
        for person in self.secret_santa.admins:
            if person.normalized_name == Person.normalize_name(name):
                return person
        return None

    async def post_dm(self, dm, message, m_type="xml"):
        """
        Post a message to the given conversation
        """
        if isinstance(message, Activity):
            return await self.connector_client.conversations.send_to_conversation(dm.id, message)
        message = MessageFactory.text(message)
        message.text_format = m_type
        return await self.connector_client.conversations.send_to_conversation(dm.id, message)

    async def post_reply(self, message, m_type="xml"):
        """
        Post a message to the given channel
        Args:
            channel: channel id to send the message to
            message: the text of the message
        """
        if isinstance(message, Activity):
            return await self.turn_context.send_activity(message)
        message = MessageFactory.text(message)
        message.text_format = m_type
        return await self.turn_context.send_activity(message)

    async def get_all_users(self):
        """
        Get all the users in the team
        """
        return self.team_members