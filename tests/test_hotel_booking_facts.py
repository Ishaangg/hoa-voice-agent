from hoa_voice_agent.services.hotel_booking_facts import BookingFactLedger


def test_first_explicit_fact_becomes_active() -> None:
    ledger = BookingFactLedger()

    proposal = ledger.propose(
        "guest_count",
        2,
        evidence="do logon ke liye",
        source_turn_id="turn-1",
        evidence_status="explicit",
    )

    assert proposal.outcome == "accepted"
    assert ledger.active_value("guest_count") == 2
    assert ledger.pending("guest_count") is None


def test_changed_fact_waits_for_confirmation_then_supersedes_old_value() -> None:
    ledger = BookingFactLedger()
    ledger.propose(
        "guest_count",
        2,
        evidence="do logon ke liye",
        source_turn_id="turn-1",
        evidence_status="explicit",
    )

    proposal = ledger.propose(
        "guest_count",
        6,
        evidence="chhe guests",
        source_turn_id="turn-2",
        evidence_status="explicit",
    )

    assert proposal.outcome == "pending_confirmation"
    assert ledger.active_value("guest_count") == 2
    assert ledger.pending("guest_count").value == 6

    committed = ledger.confirm("guest_count", accepted=True)

    assert committed.outcome == "accepted"
    assert ledger.active_value("guest_count") == 6
    assert ledger.pending("guest_count") is None
    assert [fact.status for fact in ledger.history("guest_count")] == [
        "superseded",
        "active",
    ]


def test_rejected_correction_keeps_active_value() -> None:
    ledger = BookingFactLedger()
    ledger.propose(
        "guest_count",
        2,
        evidence="two guests",
        source_turn_id="turn-1",
        evidence_status="explicit",
    )
    ledger.propose(
        "guest_count",
        6,
        evidence="six guests",
        source_turn_id="turn-2",
        evidence_status="explicit",
    )

    rejected = ledger.confirm("guest_count", accepted=False)

    assert rejected.outcome == "rejected"
    assert ledger.active_value("guest_count") == 2
    assert ledger.pending("guest_count") is None


def test_conflicting_values_do_not_replace_a_pending_correction() -> None:
    ledger = BookingFactLedger()
    ledger.propose(
        "guest_count",
        2,
        evidence="two guests",
        source_turn_id="turn-1",
        evidence_status="explicit",
    )
    ledger.propose(
        "guest_count",
        6,
        evidence="6",
        source_turn_id="turn-2",
        evidence_status="explicit",
    )

    conflict = ledger.propose(
        "guest_count",
        2,
        evidence="2",
        source_turn_id="turn-3",
        evidence_status="explicit",
    )

    assert conflict.outcome == "ambiguous"
    assert ledger.active_value("guest_count") == 2
    assert ledger.pending("guest_count").value == 6


def test_ledger_round_trips_persisted_json() -> None:
    ledger = BookingFactLedger()
    ledger.propose(
        "check_in",
        "2030-08-14",
        evidence="August 14",
        source_turn_id="turn-1",
        evidence_status="explicit",
    )
    ledger.propose(
        "check_in",
        "2030-08-15",
        evidence="actually August 15",
        source_turn_id="turn-2",
        evidence_status="explicit",
    )

    restored = BookingFactLedger.from_json(ledger.to_json())

    assert restored.active_value("check_in") == "2030-08-14"
    assert restored.pending("check_in").value == "2030-08-15"
