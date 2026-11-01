"""Spanish and Catalan opt-outs are caught without the model, and ordinary
Spanish that only looks like one is left to it."""

import pydantic
import pytest

from harvey.agents.handler import _opt_out_reason
from harvey.config import SPANISH_OPT_OUT_WORDS, ComplianceConfig
from harvey.optout_es import spanish_escalation
from tests.test_compliance_footer import _reply  # noqa: F401
from tests.test_outbox_native import state  # noqa: F401  (fixture)

SPANISH_LINE = '¿No os encaja? Responded "baja" y no volveremos a escribiros.'


@pytest.mark.parametrize("reply", [
    "Baja",
    "baja.",
    "BAJA, gracias",
    "Baja por favor",
    "Baixa",
    "No, gracias",
    "No gracias.",
    "no, muchas gracias",
    "No, gràcies",
    "No, moltes gràcies",
    "Por favor, dadme de baja.",
    "Quiero darme de baja de vuestra lista",
    "Deme de baja, gracias",
    "Solicito la baja de vuestros envíos.",
    "Quitadme de la lista, por favor",
    "Eliminadnos de vuestra base de datos",
    "Borrad mis datos.",
    "Dejad de escribirme.",
    "Dejen de enviarme correos",
    "No me escribáis más.",
    "No nos contactéis más",
    "No volváis a escribirme",
    "No me interesa.",
    "Gracias, pero no me interesa.",
    "Hola, no nos interesa. Un saludo",
    "No estamos interesados, gracias.",
    "No quiero recibir más correos",
    "Doneu-me de baixa, si us plau.",
    "No m'interessa, gràcies.",
    "No ens interessa",
    "Deixeu d'escriure'm",
    "Esborreu les meves dades",
    "No vull rebre més correus",
    "No me interesa\n\nEnviado desde mi iPhone",
], ids=lambda r: r[:30])
def test_spanish_and_catalan_opt_outs_are_caught(reply):
    assert _opt_out_reason(reply, reply), reply


@pytest.mark.parametrize("reply", [
    "Estoy de baja hasta el lunes, os contesto entonces.",   # sick leave
    "La tienda está en la planta baja.",                     # ground floor
    "En temporada baja cerramos los martes.",                # low season
    "¿El precio baja si pagamos el año entero?",             # the price goes down
    "No quiero darme de baja, pero sí cambiar de email.",    # not leaving
    "Si no me interesa ya os lo diré.",                      # conditional
    "No me interesa de momento, quizá en primavera.",        # for now
    "No me interesa el plan anual, pero el mensual sí.",     # bargaining
    "No me escribáis, mejor llamadme al móvil.",             # another channel
    "No me escribáis por aquí, por WhatsApp mejor.",
    "¿Sí o no, gracias por adelantado?",
    "Todavía no, gracias por preguntar.",                    # not yet
    "No gracias a vosotros hemos subido en Google",          # thanks to you
    "Me interesa, ¿cuándo hablamos?",
    "Igual no me interesa, depende del precio",              # maybe
], ids=lambda r: r[:30])
def test_spanish_that_only_looks_like_an_opt_out_is_left_to_the_model(reply):
    assert _opt_out_reason(reply, reply) is None, reply


def test_every_spanish_reply_an_opt_out_line_can_ask_for_is_caught():
    for word in SPANISH_OPT_OUT_WORDS:
        assert _opt_out_reason(word, word), word


def test_opt_out_line_can_ask_in_spanish_and_catalan():
    for good in (SPANISH_LINE, 'Si no te interesa, responde "baja" y no te escribo más.',
                 "Si no os interesa, contestad BAJA a este correo.",
                 'Para no recibir más correos, escríbenos «baja».',
                 "Si no us interessa, responeu \"baixa\" i no us tornarem a escriure.",
                 "¿No es para vosotros? Respondednos no, gracias, y listo."):
        assert ComplianceConfig(opt_out_line=good).opt_out_line == good
    for bad in ("Si no os interesa, ignorad este correo.",
                "Para darte de baja haz clic aquí",
                'Responde "baja" o pulsa aquí',
                "Baja: https://example.com/baja",
                "Escribe a baja@example.com"):
        with pytest.raises(pydantic.ValidationError):
            ComplianceConfig(opt_out_line=bad)


@pytest.mark.parametrize("reply", [
    "Esto lo va a ver mi abogado.",
    "Os voy a denunciar a la AEPD.",
    "Esto incumple la LSSI y el RGPD.",
    "Ho passaré al meu advocat",
])
def test_spanish_legal_threats_escalate(reply):
    assert spanish_escalation(reply)


def test_ordinary_spanish_does_not_escalate():
    assert not spanish_escalation("Gracias, ¿qué precio tiene el hosting?")


@pytest.mark.asyncio
async def test_spanish_opt_out_stops_the_lead_end_to_end(state):  # noqa: F811
    status, prompts = await _reply(state, "Gracias, pero no nos interesa. Un saludo")
    assert status == "opted_out" and prompts == []


@pytest.mark.asyncio
async def test_spanish_sick_leave_reaches_the_model(state):  # noqa: F811
    status, prompts = await _reply(state, "Estoy de baja esta semana, ¿me escribís el lunes?")
    assert status != "opted_out" and prompts
