from dataclasses import replace
from datetime import date, time

import pytest

from vonk.diff import build_changeset, live_summary, possible_conflicts
from vonk.match import Identity
from vonk.models import Event, OpFlag, OpKind, OwnEvent, Tak, mint_uid
from vonk.normalize import render_title

SCOPE = "kabouters:2026-10"
URL = "https://x/vonk-oktober-2026.pdf"
NAMES = {Tak.KABOUTERS: "Kind"}


def ev(title="Bosspel", day=11, start=time(14), end=time(17), scope=SCOPE, month=10, year=2026,
       notes="Tekst", url=URL) -> Event:  # fmt: skip
    return Event(
        scope_key=scope,
        date=date(year, month, day),
        end_date=None,
        start=start,
        end=end,
        all_day=start is None,
        title_activity=title,
        notes=f"{notes}\n{url}",
        source_url=url,
    )


def ics(summary: str) -> str:
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//t//EN\r\nBEGIN:VEVENT\r\n"
        f"UID:x\r\nDTSTAMP:20261001T000000Z\r\nSUMMARY:{summary}\r\nEND:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )


def store(events: list[Event], names=NAMES, etag="e1"):
    """Calendar + identity map as they look right after applying ``events``."""
    owns = [OwnEvent(mint_uid(), etag, e, ics(render_title(e, names))) for e in events]
    idm = {o.uid: Identity(o.event.scope_key, o.event.date, etag) for o in owns}
    return owns, idm


def kinds(cs):
    return sorted((op.kind.value, (op.after or op.before).title_activity) for op in cs.ops)


def test_identical_input_is_empty():
    desired = [ev("Bosspel", 11), ev("Koken", 31), ev("Dropping", 24, start=time(18))]
    owns, idm = store(desired)
    cs = build_changeset(SCOPE, desired, owns, idm, child_names=NAMES)
    assert cs.is_empty and cs.warnings == ()


def test_first_run_all_adds():
    cs = build_changeset(SCOPE, [ev("Bosspel", 11), ev("Koken", 31)], [], {})
    assert kinds(cs) == [("add", "Bosspel"), ("add", "Koken")]
    assert all(op.uid is None for op in cs.ops)


def test_update_on_changed_time_and_notes():
    owns, idm = store([ev("Bosspel", 11)])
    cs = build_changeset(SCOPE, [ev("Bosspel", 11, start=time(13), notes="Nieuw")], owns, idm)
    (op,) = cs.ops
    assert op.kind is OpKind.UPDATE and op.uid == owns[0].uid and op.flags == ()
    assert op.before.start == time(14) and op.after.start == time(13)


def test_title_case_and_whitespace_is_not_a_change():
    owns, idm = store([ev("Bosspel", 11)])
    assert build_changeset(SCOPE, [ev("  bosspel ", 11)], owns, idm).is_empty


def test_new_pdf_url_alone_is_not_a_change():
    owns, idm = store([ev("Bosspel", 11)])
    cs = build_changeset(SCOPE, [ev("Bosspel", 11, url="https://x/v2.pdf")], owns, idm)
    assert cs.is_empty


def test_insert_earlier_same_day_activity():
    owns, idm = store([ev("Bosspel", 11, start=time(14))])
    desired = [ev("Ontbijt", 11, start=time(9), end=time(11)), ev("Bosspel", 11)]
    cs = build_changeset(SCOPE, desired, owns, idm)
    assert kinds(cs) == [("add", "Ontbijt")]


def test_reordered_same_day_pair_only_updates():
    owns, idm = store([ev("Bosspel", 11, start=time(10)), ev("Koken", 11, start=time(14))])
    desired = [ev("Koken", 11, start=time(10)), ev("Bosspel", 11, start=time(14))]
    cs = build_changeset(SCOPE, desired, owns, idm)
    assert {op.kind for op in cs.ops} == {OpKind.UPDATE}
    by_uid = {o.uid: o.event.title_activity for o in owns}
    for op in cs.ops:
        assert by_uid[op.uid] == op.after.title_activity


def test_remove_one_of_two():
    owns, idm = store([ev("Bosspel", 11, start=time(10)), ev("Koken", 11, start=time(14))])
    cs = build_changeset(SCOPE, [ev("Koken", 11, start=time(14))], owns, idm)
    (op,) = cs.ops
    assert op.kind is OpKind.REMOVE and op.uid == owns[0].uid
    assert op.flags == ()


def test_scope_edge_31_10_to_1_11():
    # The October vonk has a 31/10 activity; November's vonk owns 1/11 on the calendar.
    nov = ev("Weekend", 1, month=11, scope="kabouters:2026-11")
    owns, idm = store([ev("Halloween", 31), nov])
    cs = build_changeset(SCOPE, [ev("Halloween", 31)], owns, idm)
    assert cs.is_empty  # the November event is not removed by the October diff
    cs = build_changeset(SCOPE, [], owns, idm)
    assert [op.uid for op in cs.ops] == [owns[0].uid]
    with pytest.raises(ValueError):
        build_changeset(SCOPE, [nov], owns, idm)


def test_moved_pair():
    owns, idm = store([ev("Bosspel", 11), ev("Koken", 18)])
    cs = build_changeset(SCOPE, [ev("Bosspel", 12), ev("Koken", 18)], owns, idm)
    assert [(op.kind, tuple(op.flags)) for op in cs.ops] == [
        (OpKind.REMOVE, (OpFlag.MOVED,)),
        (OpKind.ADD, (OpFlag.MOVED,)),
    ]
    assert cs.ops[0].before.date == date(2026, 10, 11) and cs.ops[1].after.date.day == 12


def test_supersession_newer_post_replaces_older():
    old = [ev("Bosspel", 11), ev("Koken", 18), ev("Dropping", 24)]
    owns, idm = store(old)
    newer = [ev("Bosspel", 11), ev("Koken", 18, notes="Andere tekst"), ev("Zwemmen", 25)]
    cs = build_changeset(SCOPE, newer, owns, idm)
    assert kinds(cs) == [("add", "Zwemmen"), ("remove", "Dropping"), ("update", "Koken")]
    assert cs.warnings == ()


def test_manually_edited_is_flagged():
    owns, idm = store([ev("Bosspel", 11), ev("Koken", 18)])
    edited = replace(owns[0], etag="phone", event=ev("Bosspel", 11, start=time(15)))
    cs = build_changeset(SCOPE, [ev("Bosspel", 11)], [edited, owns[1]], idm)
    flags = {op.kind: tuple(op.flags) for op in cs.ops}
    assert flags[OpKind.UPDATE] == (OpFlag.MANUALLY_EDITED,)
    assert flags[OpKind.REMOVE] == ()
    # edited + removed: still flagged
    cs = build_changeset(SCOPE, [ev("Koken", 18)], [edited, owns[1]], idm)
    (op,) = cs.ops
    assert op.kind is OpKind.REMOVE and OpFlag.MANUALLY_EDITED in op.flags


def test_rename_only_after_child_name_change():
    desired = [ev("Bosspel", 11), ev("Koken", 18)]
    owns, idm = store(desired, names={Tak.KABOUTERS: "Oud"})
    cs = build_changeset(SCOPE, desired, owns, idm, child_names={Tak.KABOUTERS: "Nieuw"})
    assert len(cs.ops) == 2
    assert all(op.kind is OpKind.UPDATE and op.flags == (OpFlag.RENAME_ONLY,) for op in cs.ops)
    assert build_changeset(SCOPE, desired, owns, idm, child_names={Tak.KABOUTERS: "Oud"}).is_empty


def test_live_summary():
    assert live_summary(ics("Kind scouts: Bosspel")) == "Kind scouts: Bosspel"
    assert live_summary("not ics") is None


def test_mass_removal_flag():
    owns, idm = store([ev("A", 3), ev("B", 10), ev("C", 17)])
    cs = build_changeset(SCOPE, [ev("A", 3)], owns, idm)
    removes = [op for op in cs.ops if op.kind is OpKind.REMOVE]
    assert len(removes) == 2
    assert all(OpFlag.MASS_REMOVAL in op.flags for op in removes)
    assert cs.warnings and "LET OP" in cs.warnings[0]
    # exactly half is not mass removal
    owns, idm = store([ev("A", 3), ev("B", 10)])
    cs = build_changeset(SCOPE, [ev("A", 3)], owns, idm)
    assert cs.warnings == () and all(OpFlag.MASS_REMOVAL not in op.flags for op in cs.ops)


def test_stripped_x_properties_matched_via_identity_map():
    desired = [ev("Bosspel", 11), ev("Koken", 18)]
    owns, idm = store(desired)
    stripped = [replace(o, event=replace(o.event, scope_key="")) for o in owns]
    assert build_changeset(SCOPE, desired, stripped, idm).is_empty


def test_empty_store_populated_calendar_adopts_and_orphans():
    desired = [ev("Bosspel", 11), ev("Koken", 18)]
    owns, _ = store(desired)
    orphan = OwnEvent(mint_uid(), "e9", replace(ev("Oud", 4), scope_key=""), ics("Oud"))
    cs = build_changeset(SCOPE, desired, [*owns, orphan], {})
    assert not any(op.kind is OpKind.ADD for op in cs.ops)
    (op,) = cs.ops
    assert op.kind is OpKind.REMOVE and op.uid == orphan.uid and op.flags == (OpFlag.ORPHAN,)


def test_possible_conflict_jaarvonk_vs_monthly():
    jaar = ev("Jonggiver en Giver trophy", 18, start=None, end=None, scope="groep:2026-2027")
    tak = ev("Jonggiver trophy", 17, start=None, end=None, scope="jeevees:2026-10")
    same_day = replace(tak, date=date(2026, 10, 18))
    far = replace(tak, date=date(2026, 12, 18))
    assert possible_conflicts([jaar], [tak]) == [(jaar, tak)]
    assert possible_conflicts([jaar], [same_day, far]) == []
    assert possible_conflicts([tak], [replace(tak, date=date(2026, 10, 18))]) == []
    cs = build_changeset("jeevees:2026-10", [tak], [], {}, related_events=[jaar])
    assert cs.ops[0].flags == (OpFlag.POSSIBLE_CONFLICT,)


def test_changeset_is_deterministic():
    owns, idm = store([ev("Bosspel", 11), ev("Koken", 18), ev("Dropping", 24)])
    desired = [ev("Zwemmen", 25), ev("Bosspel", 12), ev("Koken", 18, start=time(10))]
    a = build_changeset(SCOPE, desired, owns, idm)
    b = build_changeset(SCOPE, list(reversed(desired)), list(reversed(owns)), idm)
    assert a.content_hash() == b.content_hash()


def test_ac3_update_remove_and_inserted_earlier_activity():
    owns, idm = store([ev("Bosspel", 11), ev("Koken", 18), ev("Dropping", 24, start=time(18))])
    bosspel, koken, dropping = owns
    desired = [
        ev("Ontbijt", 11, start=time(9), end=time(11)),  # earlier same-day activity inserted
        ev("Bosspel", 11, start=time(13)),  # time changed
        ev("Koken", 18),  # untouched
    ]  # Dropping (24) is gone
    cs = build_changeset(SCOPE, desired, owns, idm)
    assert sorted((op.kind.value, op.uid) for op in cs.ops) == sorted(
        [("update", bosspel.uid), ("remove", dropping.uid), ("add", None)]
    )
    assert all(op.flags == () for op in cs.ops)
    (add,) = [op for op in cs.ops if op.kind is OpKind.ADD]
    assert add.after.title_activity == "Ontbijt"
    (upd,) = [op for op in cs.ops if op.kind is OpKind.UPDATE]
    assert upd.before.start == time(14) and upd.after.start == time(13)
    assert koken.uid not in {op.uid for op in cs.ops}  # untouched event keeps its UID
    assert cs.warnings == ()


# --- hand-edits never lead to a duplicate under the default selection -------------------


def _phone(o, **changes):
    return replace(o, etag="phone", event=replace(o.event, **changes))


def _after_default_approve(cs, owns):
    """UIDs left + events added when Jan approves the page as shown (default ticks)."""
    from vonk.web import _UNTICKED

    ticked = [op for op in cs.ops if not any(f in op.flags for f in _UNTICKED)]
    removed = {op.uid for op in ticked if op.kind is OpKind.REMOVE}
    added = [op.after for op in ticked if op.kind is OpKind.ADD]
    return [o.uid for o in owns if o.uid not in removed], added


def test_hand_edited_title_and_time_is_one_unticked_update():
    owns, idm = store([ev("Bosspel", 11), ev("Koken", 18)])
    phone = _phone(owns[0], title_activity="Bowling", start=time(10))
    cs = build_changeset(SCOPE, [ev("Bosspel", 11), ev("Koken", 18)], [phone, owns[1]], idm)
    assert [(op.kind, op.uid, op.flags) for op in cs.ops] == [
        (OpKind.UPDATE, phone.uid, (OpFlag.MANUALLY_EDITED,))
    ]
    kept, added = _after_default_approve(cs, [phone, owns[1]])
    assert len(kept) == 2 and added == []


def test_hand_edited_all_day_title_is_one_unticked_update():
    owns, idm = store([ev("Geen scouts", 11, start=None, end=None)])
    phone = _phone(owns[0], title_activity="Familiefeest")
    cs = build_changeset(SCOPE, [ev("Geen scouts", 11, start=None, end=None)], [phone], idm)
    assert [(op.kind, op.flags) for op in cs.ops] == [(OpKind.UPDATE, (OpFlag.MANUALLY_EDITED,))]


def test_hand_edited_without_counterpart_is_unticked_remove():
    owns, idm = store([ev("Bosspel", 11), ev("Koken", 18)])
    phone = _phone(owns[0], title_activity="Bowling")
    cs = build_changeset(SCOPE, [ev("Koken", 18)], [phone, owns[1]], idm)
    assert [(op.kind, op.uid, op.flags) for op in cs.ops] == [
        (OpKind.REMOVE, phone.uid, (OpFlag.MANUALLY_EDITED,))
    ]


def test_moved_hand_edited_unticks_both_sides():
    owns, idm = store([ev("Bosspel", 11), ev("Koken", 18)])
    phone = _phone(owns[0], start=time(15))
    cs = build_changeset(SCOPE, [ev("Bosspel", 12), ev("Koken", 18)], [phone, owns[1]], idm)
    assert [(op.kind, op.flags) for op in cs.ops] == [
        (OpKind.REMOVE, (OpFlag.MOVED, OpFlag.MANUALLY_EDITED)),
        (OpKind.ADD, (OpFlag.MOVED, OpFlag.MANUALLY_EDITED)),
    ]
    kept, added = _after_default_approve(cs, [phone, owns[1]])
    assert len(kept) == 2 and added == []


def _act(title, day, **kw):
    """Event whose notes are its own vonk text, as in real data."""
    return ev(title, day, notes=f"{title}: uitleg van de leiding", **kw)


@pytest.mark.parametrize(
    "change",
    [
        {"start": time(15)},  # time only
        {"title_activity": "Bowling"},  # title only
        {"title_activity": "Bowling", "start": time(10)},  # title and time
        {"date": date(2026, 10, 12)},  # moved to another day on the phone
        {"notes": "zelf aangepast"},  # notes only
    ],
)
@pytest.mark.parametrize(
    "desired",
    [
        [_act("Bosspel", 11), _act("Koken", 18)],  # vonk unchanged
        [_act("Bosspel", 11, start=time(13)), _act("Koken", 18)],  # vonk changes the time
        [_act("Bosspel", 12), _act("Koken", 18)],  # vonk moves it a day
        [_act("Ontbijt", 11, start=time(9)), _act("Bosspel", 11), _act("Koken", 18)],  # insert
    ],
)
def test_default_selection_never_duplicates_a_hand_edited_event(change, desired):
    owns, idm = store([_act("Bosspel", 11), _act("Koken", 18)])
    phone = _phone(owns[0], **change)
    cs = build_changeset(SCOPE, desired, [phone, owns[1]], idm)
    kept, added = _after_default_approve(cs, [phone, owns[1]])
    # Jan's event is kept and stands for Bosspel: at most the genuinely new Ontbijt is added.
    assert phone.uid in kept
    assert [a.title_activity for a in added] in ([], ["Ontbijt"])
    assert len(kept) + len(added) == len(desired)
