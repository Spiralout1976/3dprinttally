"""Generate a Basic-auth password hash without exposing the password in shell history."""
import getpass
from werkzeug.security import generate_password_hash

password=getpass.getpass('New 3DPrintTally password: ')
if len(password)<12:
    raise SystemExit('Use at least 12 characters.')
if password!=getpass.getpass('Repeat password: '):
    raise SystemExit('Passwords did not match.')
print('Put the following hash in .env using SINGLE QUOTES to preserve dollar signs:')
print("BASIC_AUTH_PASSWORD_HASH='"+generate_password_hash(password)+"'")
