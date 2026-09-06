"""Statistiques du dashboard. Python pur, aucune dependance de calcul."""

from typing import Any, Dict, List


def rolling_winrate(logs: List[Dict[str, Any]], window: int = 10) -> List[float]:
    """Winrate glissant, un point par combat decide."""
    decided = [log for log in logs if log.get("won") is not None]
    points = []
    for index in range(len(decided)):
        chunk = decided[max(0, index - window + 1) : index + 1]
        points.append(sum(1 for log in chunk if log["won"]) / len(chunk))
    return points


def sparkline_svg(points: List[float], width: int = 320, height: int = 90) -> str:
    """Courbe SVG inline: pas de librairie de graphes, pas de JS.

    Un graphe reste lisible sur l'ecran d'un telephone; on garde donc l'echelle
    verticale complete 0-100% plutot que de zoomer sur les variations.
    """
    if len(points) < 2:
        return '<p class="muted">Pas encore assez de combats pour tracer une courbe.</p>'

    pad = 6
    inner_w = width - 2 * pad
    inner_h = height - 2 * pad
    step = inner_w / (len(points) - 1)
    coords = [
        f"{pad + index * step:.1f},{pad + (1 - value) * inner_h:.1f}"
        for index, value in enumerate(points)
    ]
    midline = pad + 0.5 * inner_h
    return (
        f'<svg viewBox="0 0 {width} {height}" class="spark" role="img" '
        f'aria-label="Winrate glissant">'
        f'<line x1="{pad}" y1="{midline}" x2="{width - pad}" y2="{midline}" '
        f'class="spark-mid"/>'
        f'<polyline points="{" ".join(coords)}" class="spark-line"/>'
        f"</svg>"
    )


def finding_frequency(logs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Tags les plus frequents, avec severite cumulee."""
    counts: Dict[str, int] = {}
    severity: Dict[str, float] = {}
    battles: Dict[str, set] = {}

    for index, log in enumerate(logs):
        for finding in (log.get("analysis") or {}).get("findings", []):
            tag = finding["tag"]
            counts[tag] = counts.get(tag, 0) + 1
            severity[tag] = severity.get(tag, 0.0) + float(finding.get("severity", 1))
            battles.setdefault(tag, set()).add(index)

    rows = [
        {
            "tag": tag,
            "count": counts[tag],
            "severity": severity[tag],
            "battles": len(battles[tag]),
        }
        for tag in counts
    ]
    rows.sort(key=lambda row: (row["severity"], row["count"]), reverse=True)
    return rows


def summarise(logs: List[Dict[str, Any]], usage: List[Dict[str, Any]]) -> Dict[str, Any]:
    decided = [log for log in logs if log.get("won") is not None]
    wins = sum(1 for log in decided if log["won"])

    # Le journal d'usage fait foi: il couvre les deux types d'appel (analyse et
    # consolidation). S'il manque - efface, ou combats analyses avant sa mise en
    # place - on retombe sur le cout stocke dans chaque combat, qui ne couvre
    # que les analyses mais vaut mieux qu'un zero trompeur.
    cost = sum(entry.get("cost_usd", 0.0) for entry in usage)
    if not usage:
        cost = sum(
            float((log.get("analysis") or {}).get("cost_usd", 0.0) or 0.0) for log in logs
        )
    analysed = sum(1 for log in logs if log.get("analysis"))
    return {
        "battles": len(logs),
        "wins": wins,
        "losses": len(decided) - wins,
        "winrate": (wins / len(decided)) if decided else 0.0,
        "analysed": analysed,
        "api_calls": len(usage),
        "cost": cost,
        "cost_per_battle": (cost / len(logs)) if logs else 0.0,
        "avg_turns": (sum(log.get("turn_count", 0) for log in logs) / len(logs)) if logs else 0,
    }
