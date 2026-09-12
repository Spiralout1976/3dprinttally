import sqlite3
from config import DB_PATH
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    # Off by default in SQLite, per-connection, not persisted in the file — every
    # connection this app opens has to turn it on itself or the constraint below
    # is decoration, not enforcement.
    con.execute("PRAGMA foreign_keys = ON")
    return con
