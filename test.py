import database
from secretsanta import *
from bot import *

database.init_db()

p = Person.query.all()
s = SecretSanta.query.first()

