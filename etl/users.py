#!/usr/bin/env python3
"""Manages user accounts from the command line.

The first administrator has to be created here, because nobody can sign in to the
web interface yet. After that, day-to-day work happens on the /admin page.

    python3 etl/users.py list
    python3 etl/users.py add jane --admin --name "Jane Smith"
    python3 etl/users.py password jane
    python3 etl/users.py disable jane
    python3 etl/users.py delete jane
    python3 etl/users.py clear-locks --all
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from web import auth  # noqa: E402


def print_credentials(username, password):
    print("\n  " + "-" * 52)
    print(f"  username : {username}")
    print(f"  password : {password}")
    print("  " + "-" * 52)
    print("  This password will not be shown again. Pass it on;")
    print("  the user will be asked to choose their own on first sign-in.\n")


def main():
    ap = argparse.ArgumentParser(description="User account management")
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list the accounts")

    a = sub.add_parser("add", help="create an account")
    a.add_argument("username")
    a.add_argument("--name", default="")
    a.add_argument("--email", default="")
    a.add_argument("--admin", action="store_true")
    a.add_argument("--password", default="", help="generated when omitted")

    p = sub.add_parser("password", help="reset a password")
    p.add_argument("username")
    p.add_argument("--password", default="")

    c = sub.add_parser("clear-locks", help="lift a failed-login lock")
    c.add_argument("--ip", default="", help="unlock this IP")
    c.add_argument("--user", default="", help="unlock this user")
    c.add_argument("--all", action="store_true", help="clear every lock")

    for name, helptext in [("disable", "disable an account"),
                           ("enable", "re-enable an account"),
                           ("delete", "delete an account")]:
        s = sub.add_parser(name, help=helptext)
        s.add_argument("username")

    args = ap.parse_args()

    if args.command == "list":
        rows = auth.list_users()
        if not rows:
            print("No accounts yet. Create an administrator first:")
            print("  python3 etl/users.py add <username> --admin")
            return
        print(f"{'username':<20} {'role':<8} {'status':<9} {'last sign-in':<17} name")
        print("-" * 78)
        for u in rows:
            print(f"{u['username']:<20} {u['role']:<8} "
                  f"{'active' if u['active'] else 'disabled':<9} "
                  f"{(u['last_login'] or '—')[:16]:<17} {u['full_name'] or ''}")
        return

    if args.command == "clear-locks":
        if not (args.ip or args.user or args.all):
            sys.exit("Pass --ip, --user or --all.")
        n = auth.clear_locks(ip=args.ip or None, username=args.user or None)
        print(f"{n} attempt records deleted, the lock is lifted.")
        return

    target = auth.get_user(args.username.lower())

    if args.command == "add":
        password = args.password or auth.generate_password()
        try:
            auth.add_user(args.username, password, args.name, args.email,
                          "admin" if args.admin else "user",
                          created_by="command line")
        except ValueError as e:
            sys.exit(f"Error: {e}")
        print(f"'{args.username.lower()}' created"
              f"{' (administrator)' if args.admin else ''}.")
        print_credentials(args.username.lower(), password)
        return

    if not target:
        sys.exit(f"Error: '{args.username}' not found.")

    if args.command == "password":
        password = args.password or auth.generate_password()
        auth.change_password(target["id"], password, clear_flag=False)
        auth.update_user(target["id"], must_change=1)
        auth.end_all_sessions(target["id"])
        print(f"Password for '{target['username']}' reset; sessions ended.")
        print_credentials(target["username"], password)
    elif args.command in ("disable", "enable"):
        if args.command == "disable" and target["role"] == "admin" \
                and auth.admin_count() <= 1:
            sys.exit("Error: you cannot disable the last administrator.")
        auth.update_user(target["id"], active=1 if args.command == "enable" else 0)
        print(f"'{target['username']}' {args.command}d.")
    elif args.command == "delete":
        if target["role"] == "admin" and auth.admin_count() <= 1:
            sys.exit("Error: you cannot delete the last administrator.")
        auth.delete_user(target["id"])
        print(f"'{target['username']}' deleted.")


if __name__ == "__main__":
    main()
