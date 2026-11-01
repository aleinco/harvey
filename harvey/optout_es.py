"""Spanish and Catalan opt-out detection, without a model call.

Spanish and Catalan B2B email must honour a request to stop at once (LSSI
art. 21 in Spain), so these replies are caught by rules, like the English and
Hebrew ones in harvey/agents/handler.py. Each rule needs the reader to be
asking *us* to stop: "darme de baja" (unsubscribe me), never bare "baja",
which is also sick leave ("estoy de baja"), a ground floor ("planta baja") or
the low season ("temporada baja").

A match isn't final when the words before it negate or condition it ("no
quiero darme de baja", "si no me interesa...") or its sentence narrows it to
a time, a part of the offer or another channel ("no me interesa de momento",
"no me interesa el plan anual, pero...", "no me escribáis, llamadme"). The
model reads those instead, as it does for Hebrew.

All matching runs on text that is lowercased, whitespace-collapsed and
stripped of accents, so "interesa", "dádme" and "gràcies" match however the
reply was typed.
"""

from __future__ import annotations

import re
import unicodedata


def fold(text: str) -> str:
    """Lowercase, accents removed, spacing collapsed."""
    decomposed = unicodedata.normalize("NFD", text or "")
    stripped = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    return " ".join(stripped.lower().split())


_ME = r"(?:me|nos)"
_LIST = r"(?:lista|listas|listado|base de datos|bases de datos|mailing|envios|correos)"
_SEND = r"(?:escribir|enviar|mandar|contactar|llamar|molestar)"
_ES_PATTERNS = (
    # Unsubscribe me / us.
    r"(?:dar|darme|darnos|dame|danos|dadme|dadnos|deme|denos|denme|me doy|nos damos"
    r"|me quiero dar|nos queremos dar|quiero darme|queremos darnos|deseo darme"
    r"|deseamos darnos|solicito darme|solicitamos darnos) de baja",
    r"(?:solicito|solicitamos|pido|pedimos|quiero|queremos|deseo|deseamos|tramitad|tramiten)"
    r" (?:la|mi|nuestra) baja",
    r"baja (?:de|en) (?:la|las|vuestra|vuestras|su|sus|tu|tus|esta|estas) " + _LIST,
    # Take me off the list.
    r"(?:quitad|quiten|quite|quita|sacad|saquen|saque|saca|eliminad|eliminen|elimine|elimina"
    r"|borrad|borren|borre|borra)" + _ME
    + r" de (?:la|las|vuestra|vuestras|su|sus|tu|tus|esta|estas|el|vuestro|su) " + _LIST,
    # Delete my data.
    r"(?:borrad|borren|borre|borra|eliminad|eliminen|elimine|elimina|suprimid|supriman"
    r"|suprima|suprime) (?:mis|nuestros|todos mis|todos nuestros) datos",
    # Stop writing.
    r"(?:dejad|dejen|deje|deja|dejar) de " + _SEND + r"(?:me|nos|nos)?",
    r"no (?:vuelvas|vuelva|volvais|vuelvan) a " + _SEND + r"(?:me|nos)?",
    r"no " + _ME + r" (?:escribas|escriba|escribais|escriban|contactes|contacte|contacteis"
    r"|contacten|envies|envie|envieis|envien|mandes|mande|mandeis|manden|molestes"
    r"|moleste|molesteis|molesten)(?: mas)?",
    # Not interested.
    r"(?:ya )?no " + _ME + r" interesa(?:n)?",
    r"no (?:estoy|estamos) interesad[oa]s?",
    r"no (?:quiero|queremos|deseo|deseamos) (?:recibir|seguir recibiendo)",
)
_CA_PATTERNS = (
    r"(?:doneu|donin|doni|dona)-(?:me|nos) de baixa",
    r"(?:donar-me|donar-nos|em dono|ens donem|vull donar-me|volem donar-nos) de baixa",
    r"(?:demano|demanem|vull|volem|sol·licito|sol.licito|solicito) (?:la|la meva|la nostra) baixa",
    r"(?:traieu|treu|tregui|elimineu|esborreu)-(?:me|nos) de (?:la|les|vostra|la vostra)"
    r" (?:llista|llistes|base de dades)",
    r"(?:esborreu|elimineu|suprimiu) (?:les meves|les nostres) dades",
    r"(?:deixeu|deixa|deixi|deixin) d'(?:escriure|enviar|contactar|trucar)(?:-me|-nos|'m|'ns)?",
    r"no (?:m'|ens )(?:escrigueu|escriguis|contacteu|contactis|envieu|enviis|truqueu)(?: mes)?",
    r"no (?:m'|ens )interess(?:a|en)",
    r"no (?:estic|estem) interessat(?:s|da|des)?",
    r"no (?:vull|volem) (?:rebre|continuar rebent)",
)
OPT_OUT_RE = re.compile(
    r"(?<![\w'])(?:" + "|".join(_ES_PATTERNS + _CA_PATTERNS) + r")(?![\w'])")

# A match isn't final when one of the three words before it negates or
# conditions it, or its sentence narrows or bargains.
_NOT_FINAL_BEFORE = {"no", "si", "aunque", "cuando", "quizas", "igual", "sino", "quan",
                     "potser", "encara"}
_NOT_FINAL_AFTER_RE = re.compile(
    r"(?<!\w)(?:pero|aunque|sino|de momento|por ahora|ahora mismo|todavia|aun|este ano"
    r"|este mes|hasta|mas adelante|cuando|si|salvo|excepto|el plan|la oferta de|el pack"
    r"|llamad|llamame|llamadme|llamenme|telefono|whatsapp|por whatsapp|en persona"
    r"|por aqui|a este correo|a este email|perque|tot i que|de moment|per ara|encara"
    r"|truqueu|truca)(?!\w)")


# Said around an opt-out without conditioning it: "si us plau" (please) has a "si".
_COURTESY_PHRASES_RE = re.compile(r"(?<!\w)(?:si us plau|si et plau|si os parece bien)(?!\w)")


def spanish_opt_out(text: str):
    """The first Spanish or Catalan opt-out phrase that means it, or None.

    text: their reply, our quoted text already removed."""
    flat = fold(text)
    for m in OPT_OUT_RE.finditer(flat):
        before = re.sub(r"[^\w\s']", " ", flat[:m.start()]).split()[-3:]
        end = re.search(r"[.!?\n;]", flat[m.end():])
        after = flat[m.end():m.end() + end.start()] if end else flat[m.end():]
        after = _COURTESY_PHRASES_RE.sub(" ", after)
        if any(w in _NOT_FINAL_BEFORE for w in before) or _NOT_FINAL_AFTER_RE.search(after):
            continue
        return m.group(0)
    return None


# "No, gracias" / "No, moltes gràcies", one line at a time, as with "no
# thanks". Not "sí o no, gracias", "¿por qué no? gracias", "si no, gracias"
# (if not) or "todavía no, gracias" (not yet).
NO_THANKS_RE = re.compile(
    r"(?<!\w)(?<!o )(?<!que )(?<!si )(?<!todavia )(?<!aun )(?<!encara )"
    r"no[ \t,.!-]+(?:(?:muchas|moltes|mil)[ \t]+)?(?:gracias|gracies)(?!\w)"
    r"(?![ \t,]+(?:por|pero|per|a)(?!\w))")
# A reply that says only this (signature and courtesies aside) is an opt-out.
BARE_OPT_OUTS = {"baja", "de baja", "baixa", "no gracias", "no muchas gracias", "no gracies",
                 "no moltes gracies", "stop", "darme de baja"}
COURTESIES = {"gracias", "gracies", "por", "favor", "porfa", "porfavor", "saludos",
              "salutacions", "un", "saludo", "cordial", "cordialment", "atentamente"}


def spanish_no_thanks(line: str):
    """'No, gracias' on one line of their reply, or None."""
    m = NO_THANKS_RE.search(fold(line))
    return m.group(0) if m else None


def spanish_bare_opt_out(own_words: str):
    """The reply was only an opt-out word ("Baja", "No, gracias"), or None."""
    bare = re.sub(r"[^\w\s]", " ", fold(own_words))
    words = bare.split()
    for candidate in (" ".join(words), " ".join(w for w in words if w not in COURTESIES)):
        if candidate in BARE_OPT_OUTS:
            return candidate
    return None


# Words that mean a human takes over, matched as substrings of folded text.
ESCALATION_PATTERNS = (
    "abogado", "abogada", "abogados", "advocat", "denuncia", "denunciar", "denunciare",
    "aepd", "agencia de proteccion de datos", "proteccion de datos", "proteccio de dades",
    "lopd", "rgpd", "lssi", "acoso", "acosando", "assetjament", "via judicial",
    "medidas legales", "accions legals", "acciones legales",
)


def spanish_escalation(text: str) -> bool:
    flat = fold(text)
    return any(p in flat for p in ESCALATION_PATTERNS)
