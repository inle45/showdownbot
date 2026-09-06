"""Traduction des lecons en parametres. AUCUN appel API ici.

C'est le point 9 du cahier des charges, et la partie ou il faut resister a la
tentation de laisser un modele ecrire la configuration. Le pipeline est:

    findings (vocabulaire ferme, produit par le LLM)
      -> agregation deterministe sur une fenetre de N combats
      -> netting des paires antagonistes         (anti-oscillation)
      -> bande morte sur la recurrence           (anti-bruit)
      -> votes via config/tuning_rules.json      (table statique, ecrite a la main)
      -> pas bornes, clampes sur le registre     (anti-derive)
      -> proposition versionnee, approuvee ou automatique
      -> surveillance du winrate, rollback si degradation

Le LLM classe; ce fichier decide. Chaque mouvement de parametre est donc
explicable par une ligne de tuning_rules.json et un compte d'occurrences, et
reproductible a partir des memes logs.
"""

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from bot.battle_log import load_all
from bot.config import (
    BATTLES_DIR,
    HISTORY_DIR,
    PENDING_PATH,
    HeuristicConfig,
    clamp,
    load_config,
    load_taxonomy,
    load_tuning_rules,
    save_config,
)

LOGGER = logging.getLogger("showdownbot.tuner")


@dataclass
class ParamChange:
    param: str
    before: float
    after: float
    votes: List[str] = field(default_factory=list)
    rationale: str = ""

    @property
    def delta(self) -> float:
        return round(self.after - self.before, 6)

    def describe(self) -> str:
        arrow = "hausse" if self.after > self.before else "baisse"
        return (
            f"{self.param}: {self.before} -> {self.after} ({arrow}), "
            f"declenche par {', '.join(self.votes)}"
        )


@dataclass
class TuningProposal:
    created_at: str
    window_battles: int
    winrate: float
    from_version: int
    changes: List[ParamChange] = field(default_factory=list)
    net_scores: Dict[str, float] = field(default_factory=dict)
    ignored: List[str] = field(default_factory=list)
    battle_range: Tuple[str, str] = ("", "")

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["changes"] = [
            {**asdict(change), "delta": change.delta, "description": change.describe()}
            for change in self.changes
        ]
        return payload

    @property
    def is_empty(self) -> bool:
        return not self.changes


def net_scores(
    aggregate: Dict[str, Any], taxonomy: Dict[str, Any], controller: Dict[str, Any]
) -> Tuple[Dict[str, float], List[str]]:
    """Applique le netting des antagonistes et la bande morte.

    Renvoie (scores retenus, motifs d'exclusion).

    Le netting est le garde-fou central: sans lui, une fenetre ou le modele voit
    trois fois "reste trop longtemps" et deux fois "switche trop vite" ferait
    bouger le meme parametre dans les deux sens sur des cycles successifs, et le
    reglage oscillerait indefiniment.
    """
    severity = aggregate["severity"]
    battles_with = aggregate["battles_with_tag"]
    min_battles = controller["min_battles_with_tag"]
    min_net = controller["min_net_severity"]

    retained: Dict[str, float] = {}
    ignored: List[str] = []
    handled: set = set()

    for tag, score in sorted(severity.items(), key=lambda item: item[1], reverse=True):
        if tag in handled:
            continue
        spec = taxonomy.get(tag, {})
        if spec.get("polarity") != "negative":
            handled.add(tag)
            continue

        antagonist = spec.get("antagonist")
        opposing = severity.get(antagonist, 0.0) if antagonist else 0.0
        handled.add(tag)
        if antagonist:
            handled.add(antagonist)

        net = score - opposing
        if net <= 0:
            if opposing:
                ignored.append(
                    f"{tag} ({score:.0f}) annule par {antagonist} ({opposing:.0f})"
                )
            continue

        if battles_with.get(tag, 0) < min_battles:
            ignored.append(
                f"{tag}: vu dans {battles_with.get(tag, 0)} combats, "
                f"minimum requis {min_battles}"
            )
            continue

        if net < min_net:
            ignored.append(f"{tag}: severite nette {net:.1f} sous le seuil {min_net}")
            continue

        retained[tag] = net

    return retained, ignored


def _burned_edges(history_dir: str, burn_cycles: int) -> set:
    """Couples (tag, parametre) neutralises apres un rollback recent."""
    burned = set()
    if not os.path.isdir(history_dir):
        return burned
    entries = sorted(
        name for name in os.listdir(history_dir) if name.endswith(".rollback.json")
    )
    for name in entries[-burn_cycles:]:
        try:
            with open(os.path.join(history_dir, name), "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        for change in payload.get("reverted_changes", []):
            for vote in change.get("votes", []):
                burned.add((vote, change["param"]))
    return burned


def propose(
    config: Optional[HeuristicConfig] = None,
    directory: str = BATTLES_DIR,
    window: Optional[int] = None,
) -> TuningProposal:
    """Calcule les ajustements sans rien ecrire."""
    from analysis.consolidate import aggregate_findings

    config = config or load_config()
    taxonomy = load_taxonomy()
    rules_file = load_tuning_rules()
    rules = rules_file["rules"]
    controller = rules_file["controller"]
    window = window or controller["window_battles"]

    logs = [log for log in load_all(directory) if log.get("analysis")]
    recent = logs[-window:]
    aggregate = aggregate_findings(recent)

    retained, ignored = net_scores(aggregate, taxonomy, controller)
    burned = _burned_edges(HISTORY_DIR, controller["rollback"]["burn_cycles"])

    # Chaque tag vote sur des parametres; on somme les votes avant de bouger,
    # pour qu'un parametre pousse par deux tags bouge d'un pas, pas de deux.
    votes: Dict[str, float] = {}
    voters: Dict[str, List[str]] = {}
    rationales: Dict[str, List[str]] = {}
    saturation = controller["saturation_severity"]

    for tag, net in retained.items():
        strength = min(1.0, net / saturation)
        for vote in rules.get(tag, []):
            param = vote["param"]
            if (tag, param) in burned:
                ignored.append(f"{tag} -> {param}: neutralise apres un rollback recent")
                continue
            sign = 1.0 if vote["direction"] == "+" else -1.0
            votes[param] = votes.get(param, 0.0) + sign * strength * vote["weight"]
            voters.setdefault(param, []).append(tag)
            rationales.setdefault(param, []).append(vote.get("rationale", ""))

    # On ne modifie qu'une poignee de parametres par cycle: au-dela, plus aucun
    # changement n'est attribuable a une cause quand le winrate bouge.
    ranked = sorted(votes.items(), key=lambda item: abs(item[1]), reverse=True)
    limit = controller["max_params_changed_per_cycle"]

    changes: List[ParamChange] = []
    for param, vote in ranked:
        if len(changes) >= limit:
            ignored.append(f"{param}: plafond de {limit} parametres par cycle atteint")
            continue
        if abs(vote) < 1e-9:
            ignored.append(f"{param}: votes opposes exactement compenses")
            continue
        spec = config.schema[param]
        before = float(config.params[param])
        # Un pas du registre par cycle au maximum, quelle que soit la severite.
        step = spec["step"] * (1.0 if vote > 0 else -1.0)
        after = clamp(param, before + step, config.schema)
        if abs(after - before) < 1e-9:
            ignored.append(f"{param}: deja a la borne, aucun mouvement possible")
            continue
        changes.append(
            ParamChange(
                param=param,
                before=before,
                after=after,
                votes=sorted(set(voters[param])),
                rationale="; ".join(sorted(set(r for r in rationales[param] if r))),
            )
        )

    return TuningProposal(
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        window_battles=len(recent),
        winrate=aggregate["winrate"],
        from_version=config.config_version,
        changes=changes,
        net_scores=retained,
        ignored=ignored,
        battle_range=(
            recent[0].get("started_at", "") if recent else "",
            recent[-1].get("started_at", "") if recent else "",
        ),
    )


def save_pending(proposal: TuningProposal, path: str = PENDING_PATH) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(proposal.to_dict(), handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    return path


def load_pending(path: str = PENDING_PATH) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def clear_pending(path: str = PENDING_PATH) -> None:
    if os.path.exists(path):
        os.remove(path)


def apply_proposal(
    proposal: TuningProposal,
    config: Optional[HeuristicConfig] = None,
    history_dir: str = HISTORY_DIR,
) -> Dict[str, Any]:
    """Applique une proposition et archive la version precedente.

    ``save_config`` revalide contre le registre: une proposition qui produirait
    une config invalide est rejetee avant d'atteindre le disque, et le bot ne
    peut donc pas demarrer sur des reglages aberrants.
    """
    config = config or load_config()
    if proposal.is_empty:
        return {"applied": False, "reason": "aucun changement propose"}

    os.makedirs(history_dir, exist_ok=True)
    previous_version = config.config_version

    for change in proposal.changes:
        config.params[change.param] = change.after

    config.config_version = previous_version + 1
    config.derived_from = proposal.created_at
    save_config(config)

    snapshot = os.path.join(history_dir, f"{config.config_version:04d}.json")
    with open(snapshot, "w", encoding="utf-8") as handle:
        json.dump(
            {
                **config.to_dict(),
                "proposal": proposal.to_dict(),
                "applied_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "winrate_before": proposal.winrate,
                "battles_before": _battle_count(),
            },
            handle,
            indent=2,
            ensure_ascii=False,
        )
        handle.write("\n")

    clear_pending()
    LOGGER.info(
        "Config v%s -> v%s: %s",
        previous_version, config.config_version,
        "; ".join(change.describe() for change in proposal.changes),
    )
    return {
        "applied": True,
        "version": config.config_version,
        "snapshot": snapshot,
        "changes": [change.describe() for change in proposal.changes],
    }


def _battle_count(directory: str = BATTLES_DIR) -> int:
    from bot.battle_log import list_logs

    return len(list_logs(directory))


def check_rollback(
    config: Optional[HeuristicConfig] = None,
    directory: str = BATTLES_DIR,
    history_dir: str = HISTORY_DIR,
) -> Dict[str, Any]:
    """Annule le dernier changement si le winrate s'est degrade depuis.

    Frein de securite volontairement conservateur: le winrate en Random Battle
    est tres bruite, donc le rollback exige un nombre minimum de combats ET une
    chute franche. Il ne mesure pas si un changement a AIDE (impossible sur ces
    volumes), il rattrape seulement ceux qui ont visiblement nui.
    """
    config = config or load_config()
    controller = load_tuning_rules()["controller"]["rollback"]
    if not controller.get("enabled", True):
        return {"rolled_back": False, "reason": "rollback desactive"}

    snapshot_path = os.path.join(history_dir, f"{config.config_version:04d}.json")
    if not os.path.exists(snapshot_path):
        return {"rolled_back": False, "reason": "aucun instantane pour la version courante"}

    with open(snapshot_path, "r", encoding="utf-8") as handle:
        snapshot = json.load(handle)

    if not snapshot.get("proposal"):
        return {"rolled_back": False, "reason": "version initiale, rien a annuler"}

    logs = load_all(directory)
    battles_before = snapshot.get("battles_before", 0)
    since = [log for log in logs[battles_before:] if log.get("won") is not None]

    if len(since) < controller["min_battles_before_verdict"]:
        return {
            "rolled_back": False,
            "reason": f"{len(since)} combats depuis le changement, "
            f"minimum {controller['min_battles_before_verdict']} pour trancher",
            "battles_since": len(since),
        }

    window = since[: controller["evaluation_battles"]]
    winrate_after = sum(1 for log in window if log["won"]) / len(window)
    winrate_before = snapshot.get("winrate_before", 0.0)
    drop = winrate_before - winrate_after

    if drop < controller["winrate_drop_threshold"]:
        return {
            "rolled_back": False,
            "reason": f"winrate {winrate_before:.0%} -> {winrate_after:.0%}, "
            f"pas de degradation franche",
            "winrate_before": winrate_before,
            "winrate_after": winrate_after,
        }

    previous_version = config.config_version - 1
    previous_path = os.path.join(history_dir, f"{previous_version:04d}.json")
    if not os.path.exists(previous_path):
        return {"rolled_back": False, "reason": "version precedente introuvable"}

    with open(previous_path, "r", encoding="utf-8") as handle:
        previous = json.load(handle)

    reverted = snapshot["proposal"]["changes"]
    config.params = dict(previous["params"])
    config.config_version = config.config_version + 1
    config.derived_from = f"rollback de v{previous_version + 1}"
    save_config(config)

    marker = os.path.join(history_dir, f"{config.config_version:04d}.rollback.json")
    with open(marker, "w", encoding="utf-8") as handle:
        json.dump(
            {
                **config.to_dict(),
                "rolled_back_version": previous_version + 1,
                "reverted_changes": reverted,
                "winrate_before": winrate_before,
                "winrate_after": winrate_after,
                "battles_evaluated": len(window),
                "rolled_back_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
            handle,
            indent=2,
            ensure_ascii=False,
        )
        handle.write("\n")

    LOGGER.warning(
        "Rollback: winrate %.0f%% -> %.0f%% sur %s combats, retour aux reglages v%s",
        winrate_before * 100, winrate_after * 100, len(window), previous_version,
    )
    return {
        "rolled_back": True,
        "winrate_before": winrate_before,
        "winrate_after": winrate_after,
        "battles_evaluated": len(window),
        "reverted": [c["description"] for c in reverted],
        "version": config.config_version,
    }
