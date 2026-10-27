from .config import Settings
from .db import database, init_schema


def main():
    engine, _ = database(Settings.from_env().database_url)
    init_schema(engine)
    print("Demo schema initialized; use versioned migrations in production")


if __name__ == "__main__":
    main()
