"""Tiny demo app the acceptance tickets act on. Not imported by the test gate."""


def log(message: str) -> None:
    print(f"[demo] {message}")


def main() -> None:
    log("app ready")


if __name__ == "__main__":
    main()
