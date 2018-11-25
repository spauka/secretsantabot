import random
from operator import attrgetter
import re

class Person(object):
    """
    Keep track of people and their related properties
    """
    def __init__(self, name, slack_id=None, email=None, force_email=False, participant=False):
        """
        Create a person
        Args:
            name: Name of person
            slack_id: slack_id of person
            email: email address
            force_email: force sending an email to this person instead of using slack
        """
        self.name = name
        self.slack_id = slack_id
        self.email = email
        self.force_email = force_email
        self.participant = participant

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
        return cls(*data)

    def __repr__(self):
        return f"<Person '{self.name}'>"

    def __str__(self):
        return ", ".join(str(d) for d in (self.name, self.slack_id, self.email,
                                          self.force_email, self.participant))

    def __hash__(self):
        return hash(self.normalized_name)

    def __eq__(self, other):
        return self.normalized_name == other.normalized_name

    @property
    def normalized_name(self):
        """
        Return a normalized form of the name for sorting
        """
        lowername = self.name.lower()
        return re.sub('\s+', "", lowername)

    @property
    def should_email(self):
        """
        Check whether we need to email this person, or whether slack messages are OK
        """
        if self.force_email:
            return True
        if self.slack_id is None and self.email:
            return True
        return False

class SecretSanta(object):
    """
    A secret santa database that creates a secret santa gift exchange
    For a given list of names the output should be stable.

    We also support a number of actions after the list has been
    generated to remove people while minimizing disruptions.
    """
    def __init__(self, people, seed=None):
        # People must be a list of Person objects
        if not all(isinstance(x, Person) for x in people):
            raise TypeError("people must be a list of Person objects")

        # Store a sorted list of people in the secret santa
        self.people = sorted(people, key=attrgetter("normalized_name"))
        self.seed = seed

        # Generate the pairings
        random.seed(self.seed)
        self.secret_santa = random.sample(self.people, k=len(self.people))

    def has_who(self, person):
        """
        Return the person that the the requested person should buy a gift
        for
        """
        if not isinstance(person, Person):
            raise TypeError("person must be of type Person")

        i = self.secret_santa.index(person)+1
        return self.secret_santa[i%len(self.secret_santa)]

    def who_has(self, person):
        """
        Return the person who is buying a gift for the requested person
        """
        if not isinstance(person, Person):
            raise TypeError("person must be of type Person")

        i = self.secret_santa.index(person)-1
        return self.secret_santa[i%len(self.secret_santa)]

