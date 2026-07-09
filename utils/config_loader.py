from pathlib import Path
import yaml

# Root projekta
PROJECT_ROOT = Path(__file__).resolve().parent.parent

CONFIG_DIR = PROJECT_ROOT / "config"


def load_yaml(filename: str) -> dict:
    """
    Load YAML configuration from config folder.

    Example:
        rules = load_yaml("family_rules")
    """

    if not filename.endswith(".yaml"):
        filename += ".yaml"

    filepath = CONFIG_DIR / filename

    if not filepath.exists():
        raise FileNotFoundError(
            f"Configuration file not found: {filepath}"
        )

    with open(filepath, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)