from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_uses_locked_production_dependencies_and_non_root_user() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "python:3.12.13-slim-bookworm" in dockerfile
    assert "ghcr.io/astral-sh/uv:0.12.1" in dockerfile
    assert "restic/restic:0.18.1" in dockerfile
    assert "uv sync --locked --no-dev --no-install-project" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "COPY .env" not in dockerfile


def test_compose_has_persistence_healthcheck_restart_and_secret_file() -> None:
    compose = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "restart: unless-stopped" in compose
    assert '"${APP_DATA_DIRECTORY:-./data}:/app/data"' in compose
    assert '"${BACKUP_DIRECTORY:-./backups}:/backups"' in compose
    assert '["CMD", "python", "-m", "app.healthcheck"]' in compose
    assert "no-new-privileges:true" in compose
    assert "RESTIC_PASSWORD_FILE: /run/secrets/restic_password" in compose
    assert 'file: "${BACKUP_SECRET_FILE:-./secrets/restic_password.txt}"' in compose
    assert "RESTIC_PASSWORD=" not in compose


def test_runtime_data_and_secrets_are_excluded_from_git_and_build_context() -> None:
    gitignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    dockerignore = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")

    for entry in ("data/", "runtime/", "backups/", "restore/", "secrets/"):
        assert entry in gitignore
    for entry in ("data", "runtime", "backups", "restore", "secrets", ".env"):
        assert entry in dockerignore.splitlines()
