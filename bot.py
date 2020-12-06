"""
Slack chat bot for secret santa
"""
from functools import lru_cache
from operator import attrgetter
from math import ceil
import tabulate

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import Table, Column, Integer, String, Boolean, DateTime, \
        Sequence, ForeignKey
from sqlalchemy.orm import relationship, backref
from sqlalchemy.orm.exc import NoResultFound

from database import Base, db_session
from secretsanta import SecretSanta, Person, Participant

env = Environment(loader=FileSystemLoader("./templates"), autoescape=select_autoescape(['html']))

class BotDecorators:
    @staticmethod
    def ensure_admin(f):
        """
        Decorator that checks whether a user is an admin before allowing
        them to access secret information.
        """
        async def ensure_admin_wrapped(self, person, *args):
            # Run if we are an admin
            if self.check_admin(person):
                return await f(self, person, *args)

            # Otherwise print an error
            return await self.post_reply("Ho Ho No")

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
    async def say_hi(self, person, usr_msg):
        """
        First handler, just says hi
        """
        # Say hi back
        return await self.post_reply(f"Hi {person.name}.")

    async def send_allocation(self, person):
        """
        Send out an allocation to an individual with a given message.
        """
        if isinstance(person, Participant):
            person = person.participant
        return await self.send_allocation_card(person)

    async def send_allocation_to(self, person, name):
        """
        Send out an allocation to an individual.
        """
        # Find the person to send the allocation to
        participant = self.get_participant_by_name(name)
        if participant is None:
            return await self.post_reply(f"Couldn't find {name}")

        # Send their allocation
        await self.send_allocation(participant.participant)
        return await self.post_reply(f"Sent allocation to {name}.")

    @BotDecorators.ensure_admin
    async def send_all_allocations(self, person):
        """
        Send out all allocations
        """
        # Check that a drawing has been done
        if self.secret_santa is None:
            return await self.post_reply(f"No active secret santa")
        if self.secret_santa.seed is None:
            return await self.post_reply(f"Drawing hasn't been made")

        # Loop through participants and send allocations
        for participant in self.secret_santa.participants:
            # Check that the person has been drawn
            if participant.ordering is None:
                print(f"Skipping {participant}")
                await self.post_reply("Skipping {participant}")
                continue
            # Send their allocation if they have a Slack ID
            if participant.participant.chat_id is not None:
                await self.send_allocation(participant.participant)
                print(f"Sent allocation to {participant.participant.name}.")
            else:
                await self.post_reply(f"Send allocation to {participant.participant.name} manually.")
                print(f"Send allocation to {participant.participant.name} manually.")
        return await self.post_reply(f"Done sending allocations!")

    @BotDecorators.ensure_admin
    async def start_secret_santa(self, person, name):
        """
        Start up a secret santa
        """
        # Get a list of people in the team
        people = await self.get_all_users()

        # Create a new secret santa
        s = SecretSanta(name, people)
        db_session.add(s)

        # Assign it to our team
        self.secret_santa = s

        # And make the user an admin
        s.admins.append(person)

        # Done
        db_session.commit()
        return await self.post_reply(f"Started a new secret santa with name: {name}")

    @BotDecorators.ensure_admin
    async def add_person(self, person, name, email=None):
        """
        Add a person to the secret santa
        """
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
        return await self.post_reply(message)

    @BotDecorators.ensure_admin
    async def add_admin(self, op, name):
        """
        Add an admin to the secret santa
        """
        # Look up the person
        person = self.get_person_by_name(name)
        if person is None:
            return await self.post_reply(f"Couldn't find {name}")
        # Add the person to the admin list
        self.secret_santa.admins.append(person)
        db_session.commit()

        # Return status
        return await self.post_reply(f"Made {person.name} an admin")

    @BotDecorators.ensure_admin
    async def remove_person(self, op, name):
        """
        Remove a person from the secret santa
        """
        # Find a person and remove them
        person = self.get_participant_by_name(name)
        if person is None:
            return await self.post_reply(f"Couldn't find {name}")
        self.secret_santa.participants.remove(person)
        db_session.commit()

        message = f"Removed {name} from the secret santa."
        return await self.post_reply(message)

    @BotDecorators.ensure_admin
    async def do_allocations(self, op, redo=False):
        """
        Perform allocations on all participants
        """
        # Do allocations
        try:
            if redo:
                redo = True
            else:
                redo = False
            self.secret_santa.generate_ordering(force=redo)
        except RuntimeError:
            return await self.post_reply("No can do! The orderings have already been done!")
        return await self.post_reply("Ordering done!")

    @BotDecorators.ensure_admin
    async def who_has(self, op, name):
        """
        Print out who has a given person
        """
        # Look up the person
        person = self.get_person_by_name(name)
        if person is None:
            return await self.post_reply(f"Couldn't find {name}")
        if person not in self.secret_santa.participants:
            return await self.post_reply(f"{person.name} is not participating in the Secret Santa")
        # Look up the gifter
        gifter = self.secret_santa.who_has(person)

        message = f"{person.name} is getting a gift from {gifter.name}"
        return await self.post_reply(message)

    @BotDecorators.ensure_admin
    async def has_who(self, op, name):
        """
        Print out who the given person has
        """
        # Look up the person
        person = self.get_person_by_name(name)
        if person is None:
            return await self.post_reply(f"Couldn't find {name}")
        if person not in self.secret_santa.participants:
            return await self.post_reply(f"{person.name} is not participating in the Secret Santa")
        giftee = self.secret_santa.has_who(person)

        message = f"{person.name} is giving a gift to {giftee.name}"
        return await self.post_reply(message)

    async def print_me(self, person):
        """
        Print out who the given person has
        """
        return await self.send_allocation(person)

    @BotDecorators.ensure_admin
    async def print_everyone(self, op, with_allocations=False):
        """
        Print out a list of everyone, optionally with allocations.
        """
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
        await self.post_reply("People are: ")
        for i in range(ceil(len(output)/25)):
            message = tabulate.tabulate(output[i*25:(i+1)*25], headers)
            await self.post_reply(f"<pre>{message}</pre>")

    @BotDecorators.ensure_admin
    async def send_admin_help(self, op, name):
        """
        Send out the admin help message to a user
        """
        # Find the person we want to send the message to
        person = self.get_person_by_name(name)
        if person is None:
            return await self.post_reply(f"Couldn't find the user {name}...")
        if self.secret_santa and person not in self.secret_santa.admins:
            return await self.post_reply(f"User {name} is not an admin... I won't send them the help!")
        dm = await self.open_dm(person)
        message = env.get_template("admin_reveal.html").render(realname=person.name)
        await self.post_dm(dm, message)
        return await self.post_reply(f"Sent admin welcome message to {person.name}")

    @BotDecorators.ensure_admin
    async def post_welcome_message(self, op):
        """
        Post a getting started message to general
        """
        message = env.get_template("general_message.html").render()
        await self.post_to_general(message)
        return await self.post_reply("Welcome message sent!")

    async def return_help(self, op):
        """
        Give a usage string for secretsanta bot
        """
        if op is not None and self.check_admin(op):
            return await self.post_reply(env.get_template("admin_help.html").render())
        else:
            return await self.post_reply(env.get_template("help.html").render())
