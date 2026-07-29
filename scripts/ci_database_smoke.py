"""Initialize the configured database and verify the complete schema for CI."""

from __future__ import annotations

from core.database import check_database_health, init_db


def main() -> None:
    init_db()
    result = check_database_health()
    if not result.get("healthy"):
        raise SystemExit(f"Database smoke test failed: {result.get('message', 'unknown error')}")
    print(result.get("message", "Database smoke test passed."))


if __name__ == "__main__":
    main()
