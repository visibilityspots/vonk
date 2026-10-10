from dataclasses import replace
from datetime import date, time

from vonk.match import Identity, match, resolve, title_similarity
from vonk.models import Event, OwnEvent, mint_uid

SCOPE = "kabouters:2026-10"
D = date(2026, 10, 11)


def ev(title="Bosspel", day=D, start=time(14), end=time(17), scope=SCOPE) -> Event:
    return Event(scope, day, None, start, end, start is None, title, "", "")


def own(event: Event, uid: str | None = None, etag: str | None = "e1") -> OwnEvent:
    return OwnEvent(uid or mint_uid(), etag, event, "")


def ident(o: OwnEvent, scope=SCOPE, day=None) -> Identity:
    return Identity(scope, day or o.event.date, o.etag)


def titles(pairs):
    return sorted((d.title_activity, r.own.event.title_activity) for d, r in pairs)


def test_exact_match():
    a = own(ev("Bosspel"))
    r = match([ev("bosspel  ")], [a], {a.uid: ident(a)})
    assert titles(r.pairs) == [("bosspel  ", "Bosspel")]
    assert r.adds == [] and r.removes == []


def test_inserting_earlier_activity_is_one_add():
    a = own(ev("Bosspel", start=time(14)))
    r = match([ev("Ontbijt", start=time(9)), ev("Bosspel", start=time(14))], [a],
              {a.uid: ident(a)})  # fmt: skip
    assert titles(r.pairs) == [("Bosspel", "Bosspel")]
    assert [e.title_activity for e in r.adds] == ["Ontbijt"]
    assert r.removes == []


def test_reordered_pair_matches_by_title():
    a = own(ev("Bosspel", start=time(10)))
    b = own(ev("Koken", start=time(14)))
    idm = {a.uid: ident(a), b.uid: ident(b)}
    r = match([ev("Koken", start=time(10)), ev("Bosspel", start=time(14))], [a, b], idm)
    assert titles(r.pairs) == [("Bosspel", "Bosspel"), ("Koken", "Koken")]
    assert r.adds == [] and r.removes == []


def test_same_title_nearest_start():
    a = own(ev("Spel", start=time(10)))
    b = own(ev("Spel", start=time(14)))
    idm = {a.uid: ident(a), b.uid: ident(b)}
    r = match([ev("Spel", start=time(15))], [a, b], idm)
    assert r.pairs[0][1].own.uid == b.uid
    assert [x.own.uid for x in r.removes] == [a.uid]


def test_similar_title_matches():
    a = own(ev("Bosspel in het bos"))
    r = match([ev("Bosspel in t bos", start=time(13))], [a], {a.uid: ident(a)})
    assert len(r.pairs) == 1
    assert title_similarity("Bosspel in het bos", "Bosspel in t bos") >= 0.6


def test_dissimilar_title_different_time_is_add_and_remove():
    a = own(ev("Bosspel", start=time(14)))
    r = match([ev("Zwemmen", start=time(10))], [a], {a.uid: ident(a)})
    assert r.pairs == []
    assert len(r.adds) == 1 and len(r.removes) == 1


def test_same_slot_renamed_is_paired():
    a = own(ev("Bosspel", start=time(14)))
    r = match([ev("Zwemmen", start=time(14))], [a], {a.uid: ident(a)})
    assert len(r.pairs) == 1


def test_remove_one_of_two():
    a = own(ev("Bosspel", start=time(10)))
    b = own(ev("Koken", start=time(14)))
    r = match([ev("Koken", start=time(14))], [a, b], {a.uid: ident(a), b.uid: ident(b)})
    assert [x.own.uid for x in r.removes] == [a.uid]
    assert r.pairs[0][1].own.uid == b.uid


def test_identity_map_wins_over_stripped_x_properties():
    # Phone client dropped X-VONK-*: the sink gives scope_key "" but the store knows the UID.
    stripped = own(replace(ev("Bosspel"), scope_key=""))
    resolved, orphans = resolve([stripped], {stripped.uid: ident(stripped)})
    assert orphans == [] and resolved[0].source == "store"
    r = match([ev("Bosspel")], [stripped], {stripped.uid: ident(stripped)})
    assert len(r.pairs) == 1 and r.adds == [] and r.removes == []


def test_identity_map_date_used_for_grouping():
    # Jan moved the event on his phone to the 12th; the store still says the 11th.
    moved = own(ev("Bosspel", day=date(2026, 10, 12)))
    r = match([ev("Bosspel")], [moved], {moved.uid: Identity(SCOPE, D, "e0")})
    assert len(r.pairs) == 1
    assert r.pairs[0][1].manually_edited


def test_adopt_and_orphan_without_store():
    adopted = own(ev("Bosspel"))
    orphan = own(replace(ev("Koken"), scope_key=""))
    bad = own(replace(ev("Spel"), scope_key="nonsense"))
    resolved, orphans = resolve([adopted, orphan, bad], {})
    assert [x.source for x in resolved] == ["adopted"]
    assert resolved[0].last_written_etag is None and not resolved[0].manually_edited
    assert {o.uid for o in orphans} == {orphan.uid, bad.uid}


def test_plain_tuple_identity_rows():
    a = own(ev())
    r = match([ev()], [a], {a.uid: (SCOPE, D, "e1")})
    assert len(r.pairs) == 1


def test_other_scope_same_date_never_paired():
    other = own(ev("Bosspel", scope="kabouters:2026-11"))
    r = match([ev("Bosspel")], [other], {other.uid: ident(other, scope="kabouters:2026-11")})
    assert r.pairs == []
    assert len(r.adds) == 1 and len(r.removes) == 1


def test_all_day_events_match():
    a = own(ev("Geen scouts", start=None, end=None))
    r = match([ev("Geen scouts!", start=None, end=None)], [a], {a.uid: ident(a)})
    assert len(r.pairs) == 1


# --- pass 5: hand-edited events pair with the remaining desired event --------------------


def edited(o: OwnEvent, **changes) -> OwnEvent:
    """``o`` after a phone edit: new ETag, store row still has the old one."""
    return replace(o, etag="phone", event=replace(o.event, **changes))


def test_hand_edited_title_and_time_pairs_as_update():
    a = own(ev("Bosspel", start=time(14)))
    phone = edited(a, title_activity="Bowling", start=time(10))
    r = match([ev("Bosspel", start=time(14))], [phone], {a.uid: ident(a)})
    assert len(r.pairs) == 1 and r.adds == [] and r.removes == []
    assert r.pairs[0][1].manually_edited


def test_hand_edited_all_day_title_pairs_as_update():
    a = own(ev("Geen scouts", start=None, end=None))
    phone = edited(a, title_activity="Familiefeest")
    r = match([ev("Geen scouts", start=None, end=None)], [phone], {a.uid: ident(a)})
    assert len(r.pairs) == 1 and r.adds == [] and r.removes == []


def test_hand_edited_pairs_with_nearest_remaining_start():
    a = own(ev("Bosspel", start=time(14)))
    phone = edited(a, title_activity="Bowling", start=time(15))
    desired = [ev("Zwemmen", start=time(9)), ev("Koken", start=time(16))]
    r = match(desired, [phone], {a.uid: ident(a)})
    assert titles(r.pairs) == [("Koken", "Bowling")]
    assert [e.title_activity for e in r.adds] == ["Zwemmen"]


def test_hand_edited_without_counterpart_stays_remove():
    a = own(ev("Bosspel", day=D))
    phone = edited(a, title_activity="Bowling")
    r = match([ev("Bosspel", day=date(2026, 10, 18))], [phone], {a.uid: ident(a)})
    assert r.pairs == []
    assert [x.own.uid for x in r.removes] == [a.uid] and r.removes[0].manually_edited
