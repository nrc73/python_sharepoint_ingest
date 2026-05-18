from __future__ import annotations

from pathlib import Path


def _load_env_values(env_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")

    return values


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    env_example = project_root / ".env.example"
    env_file = project_root / ".env"

    if not env_example.exists():
        print("FAILED: .env.example was not found at the project root.")
        return 1

    if not env_file.exists():
        env_file.write_text(env_example.read_text(encoding="utf-8"), encoding="utf-8")
        print("Created .env from .env.example")
    else:
        print(".env already exists (left unchanged)")

    env_values = _load_env_values(env_file)
    key_vault_url = (env_values.get("KEY_VAULT_URL") or "").strip()
    if not key_vault_url:
        print(
            "FAILED: KEY_VAULT_URL is missing or empty in .env. "
            "Set KEY_VAULT_URL and re-run this command."
        )
        return 1

    print(f"KEY_VAULT_URL detected: {key_vault_url}")
    print("Environment bootstrap check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
