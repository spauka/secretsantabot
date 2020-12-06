"""
Initialize and install the secret santa database.
Otherwise, do nothing.
"""

if __name__ == "__main__":
    # Import config
    import config

    # Import the schema
    import secretsanta
    import teamsbot

    # Import the DB connection
    from database import Base, engine

    Base.metadata.create_all(bind=engine)
    print(f"Installed database to {config.database.path}.")
