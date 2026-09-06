"""Consolidation periodique: UN appel API tous les N combats.

Fusionne les analyses recentes en une fiche unique, memory/lessons_learned.md.

Point d'architecture important: cette fiche ne pilote PAS les parametres. Le
tuner travaille sur les memes findings, mais par agregation deterministe. On
donne donc au modele l'agregat deja calcule, pour que la fiche explique
exactement les chiffres qui vont bouger les reglages: la prose et les
parametres racontent la meme histoire, par construction, et non deux histoires
paralleles qui divergent.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from analysis.client import AnalysisUnavailable, AnthropicAnalyst, record_usage
from bot.battle_log import load_all
from bot.config import BATTLES_DIR, LESSONS_PATH, load_taxonomy

LOGGER = logging.getLogger("showdownbot.consolidate")


def build_consolidation_system(taxonomy: Dict[str, Any]) -> str:
    tags = "\n".join(
        f"- {tag}: {spec['description']}" for tag, spec in sorted(taxonomy.items())
    )
    return f"""Tu tiens a jour la fiche de lecons d'un bot Pokemon Showdown heuristique.

On te donne les analyses des derniers combats, l'agregat des observations sur la
fenetre, et la fiche actuelle. Tu produis la fiche mise a jour.

La fiche a un seul lecteur: la personne qui regle le bot. Elle doit pouvoir la
lire en une minute et savoir quoi corriger.

Regles:

1. Hierarchise par frequence et gravite. Un defaut vu une fois n'est pas une
   lecon; un defaut vu dans la moitie des combats en est une.

2. Distingue ce qui est recurrent de ce qui est ponctuel. Les ajustements
   automatiques se basent sur la recurrence, ta fiche doit refleter le meme
   critere.

3. Explique l'agregat chiffre qui t'est fourni, ne le contredis pas. Si un tag
   et son antagoniste apparaissent tous les deux, dis-le: c'est le signe d'un
   reglage qui oscille ou d'un diagnostic ambigu, pas d'un defaut a corriger.

4. Garde de la fiche precedente ce qui reste vrai, retire ce qui a ete corrige,
   et note explicitement les progres constates.

5. Reste factuel. Pas de conseil generique de jeu Pokemon: uniquement ce que
   ces combats montrent.

6. Ne propose aucune valeur de parametre: le reglage est fait par du code.

Vocabulaire des observations:
{tags}

Le champ lessons_markdown suit cette structure:

# Lecons apprises
_Fenetre: N combats, winrate X%_

## Defauts recurrents
Par ordre de priorite. Pour chacun: le tag, sa frequence, et ce qu'il produit
concretement en combat.

## Signaux ambigus
Tags contradictoires ou trop rares pour conclure.

## Progres
Ce qui s'est ameliore depuis la fiche precedente.

## En observation
Ce qu'il faut confirmer sur les prochains combats.
"""


CONSOLIDATION_SCHEMA = {
    "type": "object",
    "properties": {
        "lessons_markdown": {"type": "string", "description": "La fiche complete, en francais."},
        "headline": {"type": "string", "description": "Le defaut principal en une phrase."},
    },
    "required": ["lessons_markdown", "headline"],
    "additionalProperties": False,
}


def aggregate_findings(logs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Agregat deterministe des observations sur une fenetre de combats.

    C'est la meme fonction qui alimente le tuner: la fiche et les parametres
    partent donc exactement des memes chiffres.
    """
    counts: Dict[str, int] = {}
    severity: Dict[str, float] = {}
    battles_with: Dict[str, set] = {}

    for index, log in enumerate(logs):
        for finding in (log.get("analysis") or {}).get("findings", []):
            tag = finding["tag"]
            counts[tag] = counts.get(tag, 0) + 1
            severity[tag] = severity.get(tag, 0.0) + float(finding.get("severity", 1))
            battles_with.setdefault(tag, set()).add(index)

    wins = sum(1 for log in logs if log.get("won") is True)
    decided = sum(1 for log in logs if log.get("won") is not None)

    return {
        "battles": len(logs),
        "wins": wins,
        "losses": decided - wins,
        "winrate": (wins / decided) if decided else 0.0,
        "counts": counts,
        "severity": severity,
        "battles_with_tag": {tag: len(indexes) for tag, indexes in battles_with.items()},
    }


def _format_aggregate(aggregate: Dict[str, Any], taxonomy: Dict[str, Any]) -> str:
    lines = [
        f"Fenetre: {aggregate['battles']} combats, "
        f"{aggregate['wins']} victoires / {aggregate['losses']} defaites "
        f"(winrate {aggregate['winrate']:.0%})",
        "",
        "Agregat des observations (tag | combats concernes | occurrences | severite cumulee):",
    ]
    rows = sorted(
        aggregate["severity"].items(), key=lambda item: item[1], reverse=True
    )
    if not rows:
        lines.append("  aucune observation sur la fenetre")
    for tag, total in rows:
        polarity = taxonomy.get(tag, {}).get("polarity", "negative")
        antagonist = taxonomy.get(tag, {}).get("antagonist")
        note = ""
        if antagonist and antagonist in aggregate["severity"]:
            note = f"  <-- son antagoniste {antagonist} est aussi present"
        lines.append(
            f"  {tag} ({polarity}) | {aggregate['battles_with_tag'].get(tag, 0)} combats | "
            f"{aggregate['counts'].get(tag, 0)} fois | severite {total:.0f}{note}"
        )
    return "\n".join(lines)


def _format_recent(logs: List[Dict[str, Any]]) -> str:
    blocks = []
    for log in logs:
        analysis = log.get("analysis") or {}
        tags = ", ".join(
            f"{f['tag']}(s{f.get('severity')})" for f in analysis.get("findings", [])
        )
        blocks.append(
            f"- {log.get('started_at')} {log.get('result')} en {log.get('turn_count')} tours: "
            f"{analysis.get('summary', 'analyse absente')}\n  tags: {tags or 'aucun'}"
        )
    return "\n".join(blocks)


def consolidate(
    api_key: str,
    window: int = 10,
    model: str = "claude-sonnet-5",
    effort: str = "high",
    thinking: bool = False,
    directory: str = BATTLES_DIR,
    analyst: Optional[AnthropicAnalyst] = None,
) -> Dict[str, Any]:
    """Relit les ``window`` derniers combats analyses et met a jour la fiche."""
    logs = [log for log in load_all(directory) if log.get("analysis")]
    if not logs:
        raise AnalysisUnavailable("Aucun combat analyse: rien a consolider.")

    recent = logs[-window:]
    taxonomy = load_taxonomy()
    aggregate = aggregate_findings(recent)

    previous = ""
    if os.path.exists(LESSONS_PATH):
        with open(LESSONS_PATH, "r", encoding="utf-8") as handle:
            previous = handle.read()

    user_content = "\n\n".join(
        [
            "## Agregat de la fenetre",
            _format_aggregate(aggregate, taxonomy),
            "## Analyses des combats de la fenetre",
            _format_recent(recent),
            "## Fiche actuelle",
            previous or "_(aucune fiche pour l'instant, c'est la premiere consolidation)_",
        ]
    )

    analyst = analyst or AnthropicAnalyst(api_key=api_key, effort=effort, thinking=thinking)
    result = analyst.structured_call(
        model=model,
        system=build_consolidation_system(taxonomy),
        user_content=user_content,
        json_schema=CONSOLIDATION_SCHEMA,
    )

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    content = result.data["lessons_markdown"].rstrip() + (
        f"\n\n---\n\n_Fiche generee le {stamp} a partir de {len(recent)} combats "
        f"(modele {result.model}, cout ${result.cost_usd:.4f})._\n"
    )
    os.makedirs(os.path.dirname(LESSONS_PATH), exist_ok=True)
    with open(LESSONS_PATH, "w", encoding="utf-8") as handle:
        handle.write(content)

    record_usage(
        "consolidation",
        result,
        {"window": len(recent), "winrate": round(aggregate["winrate"], 3)},
    )
    LOGGER.info("Fiche de lecons mise a jour depuis %s combats ($%.4f)", len(recent), result.cost_usd)

    return {
        "path": LESSONS_PATH,
        "headline": result.data.get("headline", ""),
        "aggregate": aggregate,
        "cost_usd": result.cost_usd,
    }
