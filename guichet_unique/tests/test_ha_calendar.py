from datetime import UTC, date, datetime

from guichet_unique.ha_calendar import event_payload, make_uid, marker_of, plan_sync
from guichet_unique.models import Inscription, Service


def _ins(day: int, service: Service = Service.CANTINE) -> Inscription:
    return Inscription(service=service, jour=date(2026, 11, day))


def _as_ha(payload: dict, uid: str) -> dict:
    """Simule l'événement tel que renvoyé par GET /api/calendars (offset reformaté en UTC)."""
    utc = {k: datetime.fromisoformat(payload[k]).astimezone(UTC).isoformat()
           for k in ("start_date_time", "end_date_time")}
    return {
        "uid": uid, "summary": payload["summary"],
        "description": payload["description"].replace("\n", "\r\n"),
        "start": {"dateTime": utc["start_date_time"]},
        "end": {"dateTime": utc["end_date_time"]},
    }


def test_marker_roundtrip():
    ins = _ins(16)
    assert marker_of(event_payload(ins)["description"]) == make_uid(ins)
    assert marker_of("rendez-vous dentiste") is None
    assert marker_of(None) is None


def test_plan_sync():
    keep, change, gone, new = _ins(16), _ins(17), _ins(18), _ins(19)
    wanted = {make_uid(i): event_payload(i) for i in (keep, change, new)}
    changed_before = event_payload(change) | {"summary": "Ancien libellé"}
    existing = [
        _as_ha(wanted[make_uid(keep)], "ha-keep"),
        _as_ha(wanted[make_uid(keep)], "ha-dup"),        # doublon -> supprimé
        _as_ha(changed_before, "ha-change"),              # modifié -> supprimé + recréé
        _as_ha(event_payload(gone), "ha-gone"),           # orphelin -> supprimé
        {"uid": "perso", "summary": "Dentiste", "description": "",
         "start": {"dateTime": "2026-11-16T10:00:00+01:00"},
         "end": {"dateTime": "2026-11-16T11:00:00+01:00"}},  # pas à nous -> intact
        {"uid": "allday", "summary": "x", "description": "guichet-id: " + make_uid(gone),
         "start": {"date": "2026-11-18"}, "end": {"date": "2026-11-19"}},
    ]
    to_create, to_delete = plan_sync(wanted, existing)
    assert sorted(to_delete) == ["allday", "ha-change", "ha-dup", "ha-gone"]
    assert [marker_of(p["description"]) for p in to_create] == [make_uid(change), make_uid(new)]
