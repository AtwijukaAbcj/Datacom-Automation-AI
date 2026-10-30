import os
from pathlib import Path

from kudos.http_server import create_server, tokens_from_environment
from kudos.service import KudosService


def main() -> None:
    base_dir = Path(__file__).resolve().parent
    database_path = os.environ.get("KUDOS_DB_PATH", str(base_dir / "kudos.sqlite3"))
    host = os.environ.get("KUDOS_HOST", "127.0.0.1")
    port = int(os.environ.get("KUDOS_PORT", "8000"))
    submissions_per_hour = int(os.environ.get("KUDOS_SUBMISSIONS_PER_HOUR", "10"))
    duplicate_window_seconds = int(
        os.environ.get("KUDOS_DUPLICATE_WINDOW_SECONDS", "60")
    )

    service = KudosService(
        database_path,
        submissions_per_hour=submissions_per_hour,
        duplicate_window_seconds=duplicate_window_seconds,
    )
    service.seed_demo_employees()
    server = create_server(service, tokens_from_environment(), host, port)
    print(f"Kudos demo listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Kudos demo.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
