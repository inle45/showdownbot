"""Rendu Markdown minimal, sans dependance.

Le projet vise Termux: on evite une dependance de plus pour afficher des
fichiers qu'on genere nous-memes. Ne couvre que le sous-ensemble produit par
analysis/: titres, listes, tableaux, citations, gras, code inline, separateurs.

Tout le texte est echappe AVANT mise en forme: les rapports contiennent du
contenu issu d'un modele et de pseudos d'adversaires, qui ne doivent jamais
pouvoir injecter de HTML dans la page.
"""

import html
import re

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<![\*_])\*(?!\s)(.+?)(?<!\s)\*(?![\*])")
_CODE = re.compile(r"`([^`]+)`")


def _inline(text: str) -> str:
    text = html.escape(text)
    text = _CODE.sub(r"<code>\1</code>", text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _ITALIC.sub(r"<em>\1</em>", text)
    return text


def _flush_list(out, items):
    if items:
        out.append("<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>")
        items.clear()


def _flush_table(out, rows):
    if not rows:
        return
    header, body = rows[0], rows[2:] if len(rows) > 1 else []
    out.append('<div class="scroll"><table><thead><tr>')
    out.extend(f"<th>{cell}</th>" for cell in header)
    out.append("</tr></thead><tbody>")
    for row in body:
        out.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>")
    out.append("</tbody></table></div>")
    rows.clear()


def render(text: str) -> str:
    out = []
    items = []
    table_rows = []
    paragraph = []

    def flush_paragraph():
        if paragraph:
            out.append("<p>" + "<br>".join(paragraph) + "</p>")
            paragraph.clear()

    for raw in (text or "").splitlines():
        line = raw.rstrip()
        stripped = line.strip()

        # Ligne de continuation indentee sous une puce: elle appartient a
        # l'element courant, pas a un paragraphe suivant. Sans ce cas, le texte
        # de continuation sort AVANT la liste au moment du flush.
        if items and stripped and raw[:2] in ("  ", "\t ") and raw.startswith((" ", "\t")):
            if not stripped.startswith(("- ", "* ", "+ ", "#", ">", "|")):
                items[-1] += " " + _inline(stripped)
                continue

        if stripped.startswith("|") and stripped.endswith("|"):
            flush_paragraph()
            _flush_list(out, items)
            cells = [_inline(c.strip()) for c in stripped.strip("|").split("|")]
            table_rows.append(cells)
            continue
        _flush_table(out, table_rows)

        if not stripped:
            flush_paragraph()
            _flush_list(out, items)
            continue

        if stripped.startswith("#"):
            flush_paragraph()
            _flush_list(out, items)
            level = min(len(stripped) - len(stripped.lstrip("#")), 6)
            out.append(f"<h{level}>{_inline(stripped[level:].strip())}</h{level}>")
            continue

        if stripped in ("---", "***", "___"):
            flush_paragraph()
            _flush_list(out, items)
            out.append("<hr>")
            continue

        if stripped.startswith(("- ", "* ", "+ ")):
            flush_paragraph()
            items.append(_inline(stripped[2:]))
            continue

        if re.match(r"^\d+\.\s", stripped):
            flush_paragraph()
            items.append(_inline(re.sub(r"^\d+\.\s", "", stripped)))
            continue

        if stripped.startswith("> "):
            flush_paragraph()
            _flush_list(out, items)
            out.append(f"<blockquote>{_inline(stripped[2:])}</blockquote>")
            continue

        paragraph.append(_inline(stripped))

    flush_paragraph()
    _flush_list(out, items)
    _flush_table(out, table_rows)
    return "\n".join(out)
