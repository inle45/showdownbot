"""Construction des prompts et du schema de sortie structuree.

Deux idees portent la qualite de l'analyse:

1. On envoie la TRACE DE RAISONNEMENT du bot, pas le log Showdown brut. Le
   modele voit les scores de chaque action envisagee, donc il peut dire "le
   switch etait a 0.62 contre une attaque a 0.65, c'est la ponderation X qui a
   mal arbitre" - une phrase traduisible en parametre. Sans la trace, il ne peut
   que commenter le resultat.

2. La sortie est contrainte par un schema JSON avec enum ferme. Le modele ne
   choisit pas les parametres a modifier: il classe ce qu'il observe dans un
   vocabulaire fige, et c'est config/tuning_rules.json (ecrit a la main) qui
   traduit ces tags en ajustements.
"""

from typing import Any, Dict, List

MAX_TURNS_IN_PROMPT = 60


def build_system_prompt(taxonomy: Dict[str, Any]) -> str:
    """Prompt systeme, identique d'un appel a l'autre (donc cachable)."""
    negative = [t for t, v in taxonomy.items() if v["polarity"] == "negative"]
    positive = [t for t, v in taxonomy.items() if v["polarity"] == "positive"]

    lines = [
        "Tu analyses les parties d'un bot Pokemon Showdown en Gen 9 Random Battle.",
        "",
        "Le bot est entierement heuristique: a chaque tour il attribue un score a",
        "chaque action possible, puis joue la meilleure. On te donne ces scores et",
        "le detail de leurs composantes. Ton role est de trouver ou l'ARBITRAGE a",
        "ete mauvais, pas de commenter le resultat.",
        "",
        "Regles d'analyse, dans l'ordre d'importance:",
        "",
        "1. Juge chaque decision avec l'information dont le bot disposait A CE",
        "   MOMENT-LA. En Random Battle le set adverse est inconnu: se faire punir",
        "   par un move jamais revele n'est pas une erreur. Un pari perdu qui etait",
        "   le bon pari reste un bon choix, et une decision douteuse qui a bien fini",
        "   reste douteuse. Ne raisonne jamais a partir du resultat du combat.",
        "",
        "2. Cherche les erreurs SYSTEMATIQUES, celles qui viennent d'une ponderation",
        "   mal reglee et se repeteront. Une maladresse ponctuelle sans cause",
        "   structurelle n'a pas d'interet ici.",
        "",
        "3. Appuie chaque observation sur des tours precis et sur les scores fournis.",
        "   Si tu ne peux pas montrer le tour et le chiffre, ne la signale pas.",
        "",
        "4. Ne propose AUCUNE valeur de parametre. Classe seulement ce que tu",
        "   observes dans le vocabulaire ci-dessous; la traduction en reglages est",
        "   faite ailleurs, par du code deterministe.",
        "",
        "5. Sois severe mais honnete. Un combat correctement joue et perdu par",
        "   matchup ou par malchance doit rendre une liste 'findings' vide ou",
        "   uniquement positive. Inventer des erreurs degrade le reglage du bot.",
        "",
        "Severite: 1 = imperfection, 2 = erreur claire qui a coute du terrain,",
        "3 = erreur decisive dans l'issue du combat.",
        "",
        "Vocabulaire des observations negatives:",
    ]
    for tag in negative:
        lines.append(f"- {tag}: {taxonomy[tag]['description']}")
    lines.append("")
    lines.append("Vocabulaire des observations positives (a conserver dans le comportement):")
    for tag in positive:
        lines.append(f"- {tag}: {taxonomy[tag]['description']}")
    lines.extend(
        [
            "",
            "Le champ markdown_report est redige en francais, en Markdown, et destine",
            "a un humain qui n'a pas relu le combat. Structure attendue:",
            "",
            "## Resume",
            "Deux ou trois phrases: comment le combat s'est joue, ce qui l'a decide.",
            "",
            "## Moments cles",
            "Les tours qui ont fait basculer la partie, avec le numero de tour.",
            "",
            "## Erreurs strategiques",
            "Une puce par erreur: le tour, ce que le bot a joue, ce qu'il aurait du",
            "jouer, et quelle composante de score a mal arbitre.",
            "",
            "## Bonnes decisions",
            "Ce qui merite d'etre conserve.",
        ]
    )
    return "\n".join(lines)


def build_findings_schema(taxonomy: Dict[str, Any]) -> Dict[str, Any]:
    """Schema de sortie: l'enum rend impossible tout tag hors vocabulaire."""
    return {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "Une phrase resumant le combat.",
            },
            "markdown_report": {
                "type": "string",
                "description": "Le rapport complet en Markdown, en francais.",
            },
            "findings": {
                "type": "array",
                "description": "Observations classees. Liste vide si le combat est bien joue.",
                "items": {
                    "type": "object",
                    "properties": {
                        "tag": {"type": "string", "enum": sorted(taxonomy.keys())},
                        "severity": {"type": "integer", "enum": [1, 2, 3]},
                        "turns": {"type": "array", "items": {"type": "integer"}},
                        "note": {
                            "type": "string",
                            "description": "Justification courte, chiffrée, en francais.",
                        },
                    },
                    "required": ["tag", "severity", "turns", "note"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["summary", "markdown_report", "findings"],
        "additionalProperties": False,
    }


def _format_components(components: Dict[str, float]) -> str:
    return ", ".join(f"{name} {value:+.2f}" for name, value in components.items())


def _format_turn(turn: Dict[str, Any]) -> List[str]:
    decision = turn.get("decision") or {}
    context = decision.get("context") or {}
    chosen = decision.get("chosen")
    candidates = decision.get("candidates") or []

    if decision.get("error"):
        return [f"T{turn.get('turn')} | ERREUR MOTEUR, coup aleatoire joue"]

    header = (
        f"T{context.get('turn', turn.get('turn'))} | "
        f"{context.get('active')} {int(100 * (context.get('active_hp_fraction') or 0))}%"
        f" vs {context.get('opponent')} {int(100 * (context.get('opponent_hp_fraction') or 0))}%"
        f" | matchup {context.get('matchup'):+.1f}"
        f" | {'plus rapide' if context.get('faster') else 'plus lent'}"
        f" | degats entrants prevus {int(100 * (context.get('predicted_incoming_fraction') or 0))}%"
        f" | urgence switch {context.get('switch_urgency', 0):.2f}"
    )

    lines = [header]

    extras = []
    if context.get("active_status"):
        extras.append(f"statut {context['active_status']}")
    if context.get("active_boosts"):
        extras.append(f"mes boosts {context['active_boosts']}")
    if context.get("opponent_boosts"):
        extras.append(f"boosts adverses {context['opponent_boosts']}")
    if context.get("hazards_own_side"):
        extras.append(f"hazards chez moi {context['hazards_own_side']}")
    if context.get("opponent_revealed_moves"):
        extras.append(f"moves adverses connus: {', '.join(context['opponent_revealed_moves'])}")
    if context.get("safety_override"):
        extras.append("GARDE-FOU declenche")
    if extras:
        lines.append("     " + " | ".join(extras))

    if chosen:
        tera = " +TERA" if chosen.get("tera") else ""
        lines.append(
            f"   > JOUE {chosen['kind']} {chosen['name']}{tera} "
            f"(score {chosen['score']:.2f}: {_format_components(chosen.get('components', {}))})"
        )

    rejected = [c for c in candidates if not chosen or c["name"] != chosen["name"]]
    if rejected:
        lines.append(
            "     ecartes: "
            + " | ".join(f"{c['kind']} {c['name']} {c['score']:.2f}" for c in rejected)
        )
    return lines


def distill_battle(log: Dict[str, Any], config_params: Dict[str, Any] = None) -> str:
    """Transforme un log de combat en texte compact pour le prompt.

    On n'envoie ni le protocole Showdown brut ni le JSON complet: environ 60 a 80
    tokens par tour suffisent, l'essentiel etant les scores compares.
    """
    params = config_params if config_params is not None else log.get("config_params", {})

    header = [
        f"# Combat {log.get('battle_tag')} ({log.get('server')})",
        f"Format: {log.get('battle_format')}",
        f"Resultat: {str(log.get('result', 'inconnu')).upper()} en {log.get('turn_count')} tours",
        f"Mon equipe: {', '.join(log.get('my_team') or [])}",
        f"Equipe adverse revelee: {', '.join(log.get('opponent_team') or [])}",
        f"Survivants: moi {len(log.get('my_survivors') or [])}/{len(log.get('my_team') or [])}, "
        f"adversaire {len(log.get('opponent_survivors') or [])}/{len(log.get('opponent_team') or [])}",
        "",
        "## Reglages du moteur pour ce combat",
        "  ".join(f"{k}={v}" for k, v in sorted(params.items())),
        "",
        "## Deroule tour par tour",
        "Format: etat vu par le bot, puis l'action jouee avec le detail de son",
        "score, puis les actions ecartees avec le leur.",
        "",
    ]

    turns = log.get("turns") or []
    if len(turns) > MAX_TURNS_IN_PROMPT:
        # Combat exceptionnellement long: on garde le debut et la fin, la ou se
        # jouent l'installation et la conclusion.
        keep = MAX_TURNS_IN_PROMPT // 2
        selected = turns[:keep] + turns[-keep:]
        elided = len(turns) - len(selected)
    else:
        selected = turns
        elided = 0

    body: List[str] = []
    for index, turn in enumerate(selected):
        if elided and index == MAX_TURNS_IN_PROMPT // 2:
            body.append(f"\n[... {elided} tours intermediaires omis ...]\n")
        body.extend(_format_turn(turn))

    return "\n".join(header + body)
