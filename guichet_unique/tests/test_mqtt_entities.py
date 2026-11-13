from datetime import date

from guichet_unique.mqtt_entities import compute_states
from guichet_unique.web import _Breakdown


def test_compute_states():
    inscriptions = [
        {"service": "cantine", "jour": "2026-11-13"},
        {"service": "cantine", "jour": "2026-11-16"},
        {"service": "periscolaire_soir", "jour": "2026-11-14"},
    ]
    month = _Breakdown(2026, 11, 12.5, [
        {"service_value": "cantine", "count": 2, "total": 9.0},
        {"service_value": "periscolaire_soir", "count": 1, "total": 3.5},
    ], [], 3)
    prev = _Breakdown(2026, 10, 40.0, [], [], 0)

    entities, state = compute_states(
        inscriptions, month, prev, "2026-11-13T06:00:00", date(2026, 11, 13),
    )

    assert state["cantine_aujourdhui"] == "ON"
    assert state["cantine_demain"] == "OFF"
    assert state["periscolaire_soir_aujourdhui"] == "OFF"
    assert state["periscolaire_soir_demain"] == "ON"
    assert state["cantine_mois"] == 2
    assert state["periscolaire_soir_cout_mois"] == 3.5
    assert state["cout_mois"] == 12.5
    assert state["cout_mois_precedent"] == 40.0
    assert state["derniere_sync"].startswith("2026-11-13T06:00:00")
    assert set(entities) == set(state)
    assert entities["cantine_aujourdhui"][0] == "binary_sensor"
