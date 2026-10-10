from __future__ import annotations

from datetime import UTC, date, datetime, time

import pytest

from vonk.models import (
    CATEGORY_TO_TAK,
    UID_DOMAIN,
    UID_PREFIX,
    ChangeSet,
    Event,
    ExtractedSpan,
    Operation,
    OpFlag,
    OpKind,
    OpResult,
    OwnEvent,
    Scope,
    Sink,
    Tak,
    is_own_uid,
    mint_uid,
)

URL = "https://scoutstervant.be/wp-content/uploads/2026/09/vonk-jevees-oktober-2026.pdf"


def ev(**kw) -> Event:
    base = dict(
        scope_key="jeevees:2026-10",
        date=date(2026, 10, 24),
        end_date=None,
        start=time(18, 0),
        end=None,
        all_day=False,
        title_activity="Dropping",
        notes="Zaterdag 24 oktober\n18u - ....\nDropping",
        source_url=URL,
        end_unknown=True,
    )
    base.update(kw)
    return Event(**base)


def test_category_map_matches_recorded_wp_categories():
    import json
    from pathlib import Path

    cats = json.loads(
        (Path(__file__).parents[1] / "fixtures" / "wp" / "categories.json").read_text()
    )
    by_id = {c["id"]: c["name"] for c in cats}
    names = {
        Tak.KAPOENEN: "Vonk kapoenen",
        Tak.KABOUTERS: "Vonk kabouters",
        Tak.WELPEN: "Vonk welpen",
        Tak.JOGIS: "Vonk jong gidsen",
        Tak.JEEVEES: "Vonk jong verkenners",
        Tak.GIDSEN: "Vonk gidsen",
        Tak.VERKENNERS: "Vonk verkenners",
        Tak.JINS: "Vonk jins",
    }
    assert set(CATEGORY_TO_TAK) == set(range(9, 17))
    for cid, tak in CATEGORY_TO_TAK.items():
        assert by_id[cid] == names[tak]


def test_uid_ownership():
    uid = mint_uid()
    assert uid.startswith(UID_PREFIX) and uid.endswith(UID_DOMAIN)
    assert is_own_uid(uid)
    assert mint_uid() != uid
    for foreign in (
        None,
        "",
        "abc@example.com",
        "vonk-x@scoutstervant",
        "VONK-x@vonk.visibilityspots",
        f"{UID_PREFIX}{UID_DOMAIN}",
        f"x{uid}",
        f"{uid}.evil",
        "vonk-a b@vonk.visibilityspots",
    ):
        assert not is_own_uid(foreign), foreign


@pytest.mark.parametrize(
    ("scope", "key"),
    [
        (Scope(Tak.KABOUTERS, "2026-10"), "kabouters:2026-10"),
        (Scope.monthly(Tak.JEEVEES, 2027, 1), "jeevees:2027-01"),
        (Scope.scout_year(2026), "groep:2026-2027"),
    ],
)
def test_scope_key_roundtrip(scope, key):
    assert scope.key == key
    assert Scope.parse(key) == scope


@pytest.mark.parametrize(
    "key", ["kabouters", "scouts:2026-10", "kabouters:2026-13", "groep:2026-2028", "groep:26-27"]
)
def test_scope_parse_rejects(key):
    with pytest.raises(ValueError):
        Scope.parse(key)


def test_scope_is_scout_year():
    assert Scope.scout_year(2026).is_scout_year
    assert not Scope.monthly(Tak.KABOUTERS, 2026, 10).is_scout_year


def test_event_roundtrip_and_validation():
    e = ev(warnings=("weekday mismatch",))
    assert Event.from_dict(e.to_dict()) == e
    allday = ev(start=None, all_day=True, end_unknown=False, end_date=date(2026, 10, 25))
    assert Event.from_dict(allday.to_dict()) == allday
    with pytest.raises(ValueError):
        ev(all_day=True)
    with pytest.raises(ValueError):
        ev(end_date=date(2026, 10, 1))


def test_span_roundtrip():
    s = ExtractedSpan("Zaterdag 24 oktober", None, "18u", None, "Dropping", None, False)
    assert ExtractedSpan.from_dict(s.to_dict()) == s
    assert s.spans() == ("Zaterdag 24 oktober", "18u", "Dropping")


def test_own_event_rejects_foreign_uid():
    with pytest.raises(ValueError):
        OwnEvent("foreign@example.com", None, ev(), "")


def test_operation_shape_rules():
    uid = mint_uid()
    Operation(OpKind.ADD, None, None, ev())
    Operation(OpKind.UPDATE, uid, ev(), ev(title_activity="Dropping!"))
    Operation(OpKind.REMOVE, uid, ev(), None, (OpFlag.ORPHAN,))
    with pytest.raises(ValueError):
        Operation(OpKind.ADD, None, ev(), ev())
    with pytest.raises(ValueError):
        Operation(OpKind.REMOVE, "foreign@example.com", ev(), None)
    with pytest.raises(ValueError):
        Operation(OpKind.UPDATE, uid, None, ev())


def test_changeset_hash_is_deterministic_and_content_bound():
    uid = "vonk-00000000-0000-4000-8000-000000000000@vonk.visibilityspots"
    ops = (
        Operation(OpKind.ADD, None, None, ev()),
        Operation(OpKind.REMOVE, uid, ev(date=date(2026, 10, 31)), None, (OpFlag.MOVED,)),
    )
    t1 = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    t2 = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    a = ChangeSet("a", "jeevees:2026-10", ops, (), t1)
    b = ChangeSet("b", "jeevees:2026-10", ops, ("w",), t2)
    assert a.content_hash() == b.content_hash()  # id/time/warnings not part of the hash
    assert len(a.content_hash()) == 64
    c = ChangeSet("c", "jeevees:2026-10", ops[::-1], (), t1)
    assert c.content_hash() != a.content_hash()
    d = ChangeSet("d", "jeevees:2026-10", (Operation(OpKind.ADD, None, None, ev(end=time(21))),))
    assert d.content_hash() != ChangeSet("e", "jeevees:2026-10", ops[:1]).content_hash()
    assert ChangeSet.from_dict(a.to_dict()) == a
    assert ChangeSet.from_dict(a.to_dict()).content_hash() == a.content_hash()


def test_changeset_requires_aware_datetime():
    with pytest.raises(ValueError):
        ChangeSet("x", "jeevees:2026-10", (), (), datetime(2026, 10, 9))


def test_sink_protocol_runtime_checkable():
    class Fake:
        def list_own(self, scope_key):
            return []

        def apply(self, changeset, selected_op_indexes):
            return [OpResult(i, True, 201, '"etag"', None) for i in selected_op_indexes]

    assert isinstance(Fake(), Sink)
    assert not isinstance(object(), Sink)
