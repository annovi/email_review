"""
Terminal UI utilities.
"""

import os
import textwrap


def clear_screen():
    """Clear the terminal screen."""
    os.system("cls" if os.name == "nt" else "clear")


def divider(char: str = "─", width: int = 70):
    """Print a divider line."""
    print(char * width)


def print_banner(title: str = "Email Reviewer", subtitle: str = ""):
    """Print the application banner."""
    clear_screen()
    divider("═")
    print(f"  📬  {title}")
    if subtitle:
        print(f"  {subtitle}")
    divider("═")
    print()


def print_wrapped(text: str, indent: int = 2, width: int = 68):
    """Print text wrapped to terminal width."""
    prefix = " " * indent
    for line in textwrap.wrap(text, width):
        print(f"{prefix}{line}")


def print_menu(options: list, title: str = ""):
    """Print a menu with numbered options."""
    if title:
        print(f"  {title}")
        divider()
    
    for i, (key, label) in enumerate(options):
        print(f"  [{key}] {label}")
    print()


def get_choice(prompt: str = "Choice: ", valid: list = None) -> str:
    """Get user input with optional validation."""
    while True:
        choice = input(f"  {prompt}").strip().lower()
        if valid is None or choice in valid:
            return choice
        print(f"  Please enter one of: {', '.join(valid)}")


def confirm(prompt: str) -> bool:
    """Ask for yes/no confirmation."""
    response = input(f"  {prompt} (yes/no): ").strip().lower()
    return response in ("yes", "y")
