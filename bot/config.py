"""Chargement et validation de la configuration heuristique.

Le registre (config/heuristics.schema.json) est la source de verite: le moteur
refuse de demarrer si config/heuristics.json contient une cle inconnue ou une
valeur hors bornes. C'est ce qui rend le tuner automatique sur (point 9): il ne
peut produire qu'une config que ce module accepte, sinon le bot ne part pas.
"""

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "config")
SCHEMA_PATH = os.path.join(CONFIG_DIR, "heuristics.schema.json")
CONFIG_PATH = os.path.join(CONFIG_DIR, "heuristics.json")
HISTORY_DIR = os.path.join(CONFIG_DIR, "history")
TAXONOMY_PATH = os.path.join(CONFIG_DIR, "finding_taxonomy.json")
TUNING_RULES_PATH = os.path.join(CONFIG_DIR, "tuning_rules.json")
PENDING_PATH = os.path.join(CONFIG_DIR, "pending_changes.json")

MEMORY_DIR = os.path.join(ROOT, "memory")
BATTLES_DIR = os.path.join(MEMORY_DIR, "battles")
LESSONS_PATH = os.path.join(MEMORY_DIR, "lessons_learned.md")
UNMAPPED_PATH = os.path.join(MEMORY_DIR, "unmapped_findings.md")
USAGE_PATH = os.path.join(MEMORY_DIR, "api_usage.jsonl")


class ConfigError(ValueError):
    """Configuration invalide: cle inconnue, type incorrect ou valeur hors bornes."""


def _read_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


@dataclass
class HeuristicConfig:
    """Parametres du moteur, valides contre le registre."""

    params: Dict[str, Any]
    schema: Dict[str, Any]
    config_version: int = 1
    derived_from: Optional[str] = None
    path: str = CONFIG_PATH

    def __getitem__(self, key: str) -> Any:
        return self.params[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)

    def bounds(self, key: str):
        spec = self.schema[key]
        return spec.get("min"), spec.get("max"), spec.get("step")

    def adjustable_params(self) -> List[str]:
        """Parametres que le tuner peut deplacer (les flottants bornes)."""
        return sorted(k for k, v in self.schema.items() if v["type"] == "float")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "config_version": self.config_version,
            "derived_from": self.derived_from,
            "params": dict(self.params),
        }


def load_schema(path: str = SCHEMA_PATH) -> Dict[str, Any]:
    return _read_json(path)["params"]


def validate(params: Dict[str, Any], schema: Dict[str, Any]) -> List[str]:
    """Renvoie la liste des problemes. Vide = configuration valide."""
    problems: List[str] = []

    for key in params:
        if key not in schema:
            problems.append(f"parametre inconnu: {key!r} (absent du registre)")

    for key, spec in schema.items():
        if key not in params:
            problems.append(f"parametre manquant: {key!r}")
            continue
        value = params[key]
        if spec["type"] == "bool":
            if not isinstance(value, bool):
                problems.append(f"{key}: booleen attendu, recu {type(value).__name__}")
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            problems.append(f"{key}: nombre attendu, recu {type(value).__name__}")
            continue
        low, high = spec["min"], spec["max"]
        if not (low - 1e-9 <= value <= high + 1e-9):
            problems.append(f"{key}: {value} hors bornes [{low}, {high}]")

    return problems


def clamp(key: str, value: float, schema: Dict[str, Any]) -> float:
    """Ramene une valeur dans les bornes et l'aligne sur le pas du registre."""
    spec = schema[key]
    if spec["type"] == "bool":
        return bool(value)
    low, high, step = spec["min"], spec["max"], spec.get("step")
    value = max(low, min(high, value))
    if step:
        value = round(round((value - low) / step) * step + low, 10)
        value = max(low, min(high, value))
    return round(value, 6)


def load_config(path: str = CONFIG_PATH, schema_path: str = SCHEMA_PATH) -> HeuristicConfig:
    schema = load_schema(schema_path)
    raw = _read_json(path)
    params = raw.get("params", {})
    problems = validate(params, schema)
    if problems:
        raise ConfigError(
            "Configuration heuristique invalide dans %s:\n  - %s" % (path, "\n  - ".join(problems))
        )
    return HeuristicConfig(
        params=params,
        schema=schema,
        config_version=raw.get("config_version", 1),
        derived_from=raw.get("derived_from"),
        path=path,
    )


def save_config(config: HeuristicConfig, path: Optional[str] = None) -> None:
    """Ecrit la config apres re-validation. Refuse d'ecrire un fichier invalide."""
    problems = validate(config.params, config.schema)
    if problems:
        raise ConfigError("Refus d'ecrire une config invalide:\n  - " + "\n  - ".join(problems))
    target = path or config.path
    payload = config.to_dict()
    payload["_comment"] = (
        "Valeurs actives du moteur heuristique. Modifiable a la main OU par "
        "analysis/tuner.py. Toute cle doit exister dans heuristics.schema.json."
    )
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def load_taxonomy(path: str = TAXONOMY_PATH) -> Dict[str, Any]:
    return _read_json(path)["findings"]


def load_tuning_rules(path: str = TUNING_RULES_PATH) -> Dict[str, Any]:
    return _read_json(path)


@dataclass
class Settings:
    """Configuration d'execution, lue depuis l'environnement (.env)."""

    username: str = ""
    password: str = ""
    server: str = "local"
    battle_format: str = "gen9randombattle"
    anthropic_api_key: str = ""
    analysis_model: str = "claude-sonnet-5"
    consolidation_model: str = "claude-sonnet-5"
    analysis_effort: str = "high"
    analysis_thinking: bool = False
    consolidate_every: int = 10
    tuning_mode: str = "propose"
    enable_ladder: bool = False
    termux_notifications: bool = False
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def analysis_enabled(self) -> bool:
        return bool(self.anthropic_api_key)


def _as_bool(value: str, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "oui"}


def load_settings(env_path: Optional[str] = None) -> Settings:
    """Charge le .env s'il existe, sans ecraser les variables deja exportees."""
    path = env_path or os.path.join(ROOT, ".env")
    if os.path.exists(path):
        try:
            from dotenv import load_dotenv

            load_dotenv(path, override=False)
        except ImportError:  # repli sans python-dotenv
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())

    return Settings(
        username=os.environ.get("SHOWDOWN_USERNAME", ""),
        password=os.environ.get("SHOWDOWN_PASSWORD", ""),
        server=os.environ.get("SHOWDOWN_SERVER", "local").strip().lower(),
        battle_format=os.environ.get("BATTLE_FORMAT", "gen9randombattle"),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        analysis_model=os.environ.get("ANALYSIS_MODEL", "claude-sonnet-5"),
        consolidation_model=os.environ.get("CONSOLIDATION_MODEL", "claude-sonnet-5"),
        analysis_effort=os.environ.get("ANALYSIS_EFFORT", "high"),
        analysis_thinking=_as_bool(os.environ.get("ANALYSIS_THINKING", "")),
        consolidate_every=int(os.environ.get("CONSOLIDATE_EVERY", "10") or 10),
        tuning_mode=os.environ.get("TUNING_MODE", "propose").strip().lower(),
        enable_ladder=_as_bool(os.environ.get("ENABLE_LADDER", "")),
        termux_notifications=_as_bool(os.environ.get("TERMUX_NOTIFICATIONS", "")),
    )
