import random
from datetime import datetime
from operator import attrgetter
import re
import asyncio

from sqlalchemy import Table, Column, Integer, String, Boolean, DateTime, \
        Sequence, ForeignKey
from sqlalchemy.orm import relationship
from database import Base, db_session

# Create association table to allow secret-santa instances to associate to a list of people
class Participant(Base):
    __tablename__ = "participants"
    secretsanta_id = Column(Integer, ForeignKey('secretsanta.id'), primary_key=True)
    person_id = Column(Integer, ForeignKey('person.id'), primary_key=True)
    ordering = Column(Integer)
    seen = Column(DateTime)
    participant = relationship("Person", back_populates="secret_santas", sync_backref=False)
    secret_santa = relationship("SecretSanta", back_populates="participants")

    def __repr__(self):
        return f"<Participant '{self.participant.name}' in SecretSanta '{self.secret_santa.name}' (seen: {self.seen})>"

# Create a list of admins for this secret santa
secret_santa_admins = Table("admins", Base.metadata,
        Column("secretsanta_id", ForeignKey("secretsanta.id"), primary_key=True),
        Column("person_id", ForeignKey("person.id"), primary_key=True))

class Person(Base):
    """
    Keep track of people and their related properties
    """
    __tablename__ = 'person'

    id = Column(Integer, Sequence("person_id_seq"), primary_key=True)
    name = Column(String)
    chat_source = Column(String)
    chat_id = Column(String)
    email = Column(String)
    force_email = Column(Boolean)
    team_id = Column(Integer, ForeignKey("teamsbot.team_id"))
    team = relationship("TeamsSecretSantaBase", back_populates="team_members", foreign_keys=[team_id])
    secret_santas = relationship(Participant, back_populates="participant", viewonly=True)
    administers = relationship("SecretSanta", secondary=secret_santa_admins, back_populates="admins", viewonly=True)

    def __init__(self, name, chat_source, chat_id, email, force_email=False):
        self.name = name
        self.chat_source = chat_source
        self.chat_id = chat_id
        self.email = email
        self.force_email = force_email

    @classmethod
    def from_str(cls, str_repr, delim=",", strip_whitespace=True):
        """
        Create a person from their string representation
        """
        data = str_repr.split(delim)
        if strip_whitespace:
            data = [s.strip() for s in data]
        # Convert use_email and participant to bool
        data[3] = True if data[3].lower() == "true" else False
        data[4] = True if data[4].lower() == "true" else False
        data[5] = True if data[5].lower() == "true" else False
        return cls(*data)

    @classmethod
    def find_by_chat_id(cls, chat_source, chat_id):
        """
        Find a person by their chat id
        """
        return cls.query.filter(cls.chat_source.ilike(chat_source), cls.chat_id.ilike(chat_id)).one_or_none()

    def __repr__(self):
        return f"<Person '{self.name}'>"

    def __str__(self):
        return ", ".join(str(d) for d in (self.name, self.chat_source, self.chat_id, self.email,
                                          self.force_email))

    def __hash__(self):
        return hash(self.normalized_name)

    def __eq__(self, other):
        if isinstance(other, Participant):
            other = other.participant
        if not isinstance(other, Person):
            return False
        return self.normalized_name == other.normalized_name

    @classmethod
    def normalize_name(cls, name):
        """
        Return a normalized for of the name
        """
        lowername = name.lower()
        return re.sub('\s+', "", lowername)

    @property
    def normalized_name(self):
        """
        Return a normalized form of the name for sorting
        """
        return self.normalize_name(self.name)

    @property
    def should_email(self):
        """
        Check whether we need to email this person, or whether slack messages are OK
        """
        if self.force_email:
            return True
        if self.chat_id is None and self.email:
            return True
        return False

class SecretSanta(Base):
    """
    A secret santa database that creates a secret santa gift exchange.

    We also support a number of actions after the list has been
    generated to remove people while minimizing disruptions.
    """
    __tablename__ = 'secretsanta'

    id = Column(Integer, Sequence("secretsanta_id_seq"), primary_key=True)
    name = Column(String)
    seed = Column(Integer)
    participants = relationship(Participant, back_populates="secret_santa", cascade="save-update,merge,delete,delete-orphan")
    admins = relationship(Person, secondary=secret_santa_admins, back_populates="administers", sync_backref=False)

    def __init__(self, name, people, seed=None):
        self.name = name
        self.seed = seed

        # People must be a list of Person objects
        if not all(isinstance(x, Person) for x in people):
            raise TypeError("people must be a list of Person objects")
        # Store a sorted list of people in the secret santa
        for person in people:
            self.add_participant(person)
        db_session.commit()

    def add_participant(self, person):
        """
        Add a person to the secret santa. This is now safe to do after the ordering has been generated.
        """
        print(f"Adding participant {person}")
        participant = Participant(participant=person)
        self.participants.append(participant)
        db_session.commit()

    def generate_ordering(self, force=False, reset_seen=True):
        """
        Generate the ordering for the secret santa, if it has not already been done.
        """
        # First, double check that the ordering has not already been done
        if not force:
            for person in self.participants:
                if person.ordering is not None:
                    raise RuntimeError("Attempting to redo orderings for a secret santa that has already been drawn")

        # Get a sorted list of participants
        sorted_participants = sorted(self.participants, key=attrgetter("participant.normalized_name"))

        # Then, generate the orderings using the seed
        if self.seed is None or force:
            self.seed = random.randint(0, 4_294_967_295)
        random.seed(self.seed)
        sorted_participants = random.sample(sorted_participants, k=len(sorted_participants))

        # And insert them back into the database
        for place, participant in enumerate(sorted_participants):
            participant.ordering = place*10
            if reset_seen:
                participant.seen = None
        db_session.commit()

    def update_seen(self, person):
        """
        Update the seen indicator for a given person
        """
        for participant in self.participants:
            if participant.participant == person:
                participant.seen = datetime.now()
                db_session.commit()
                break
        else:
            raise IndexError("Can't find person {person}")

    def get_ordered_list(self):
        """
        Return the ordered list of participants
        """
        participants = (p for p in self.participants if p.ordering is not None)
        return [p.participant for p in sorted(participants, key=attrgetter("ordering"))]

    def has_who(self, person):
        """
        Return the person that the the requested person should buy a gift
        for
        """
        if not isinstance(person, Person):
            raise TypeError("person must be of type Person")

        secret_santa = self.get_ordered_list()
        i = secret_santa.index(person)+1
        return secret_santa[i%len(secret_santa)]

    def who_has(self, person):
        """
        Return the person who is buying a gift for the requested person
        """
        if not isinstance(person, Person):
            raise TypeError("person must be of type Person")

        secret_santa = self.get_ordered_list()
        i = secret_santa.index(person)-1
        return secret_santa[i%len(secret_santa)]

