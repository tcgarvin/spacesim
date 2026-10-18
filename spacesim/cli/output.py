"""Output formatting utilities."""


def print_success(message: str) -> None:
    """Print a success message."""
    print(f"✓ {message}")


def print_error(message: str) -> None:
    """Print an error message."""
    print(f"✗ {message}")


def print_warning(message: str) -> None:
    """Print a warning message."""
    print(f"⚠ {message}")


def print_section(title: str) -> None:
    """Print a section divider."""
    print(f"\n{'=' * 60}")
    if title:
        print(f" {title}")
        print("=" * 60)
