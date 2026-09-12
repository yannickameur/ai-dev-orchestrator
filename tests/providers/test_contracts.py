"""Tests for the normalized provider-state contracts (Phase 1 / Slice 0).

These are pure, offline tests of the domain types themselves: no Claude,
no Codex, no Ralph, no network. Fixtures captured from real provider
responses belong to later slices (adapter implementations).
"""

from datetime import datetime, timedelta, timezone

import pytest

from orchestrator.providers.contracts import (
    ProviderAvailability,
    ProviderState,
    QuotaWindow,
    ResetCredit,
    ResetCreditStatus,
    UnavailabilityReason,
)

UTC_NOW = datetime(2026, 9, 12, 15, 0, tzinfo=timezone.utc)
NAIVE_NOW = datetime(2026, 9, 12, 15, 0)


def _available(observed_at: datetime = UTC_NOW) -> ProviderAvailability:
    return ProviderAvailability(available=True, observed_at=observed_at)


class TestProviderAvailability:
    def test_available_provider_has_no_reason(self) -> None:
        availability = ProviderAvailability(available=True, observed_at=UTC_NOW)
        assert availability.available is True
        assert availability.reason is None

    def test_unavailable_provider_requires_a_reason(self) -> None:
        with pytest.raises(ValueError, match="reason is required"):
            ProviderAvailability(available=False, observed_at=UTC_NOW)

    def test_unavailable_provider_with_unknown_reason(self) -> None:
        availability = ProviderAvailability(
            available=False,
            observed_at=UTC_NOW,
            reason=UnavailabilityReason.UNKNOWN,
        )
        assert availability.available is False
        assert availability.reason is UnavailabilityReason.UNKNOWN

    def test_unavailable_provider_quota_exhausted(self) -> None:
        # Mirrors Codex's ordinaryUsageAllowed=false / rate_limit_reached.
        availability = ProviderAvailability(
            available=False,
            observed_at=UTC_NOW,
            reason=UnavailabilityReason.QUOTA_EXHAUSTED,
        )
        assert availability.reason is UnavailabilityReason.QUOTA_EXHAUSTED

    def test_available_provider_cannot_carry_a_reason(self) -> None:
        with pytest.raises(ValueError, match="must be None when available=True"):
            ProviderAvailability(
                available=True,
                observed_at=UTC_NOW,
                reason=UnavailabilityReason.UNKNOWN,
            )

    def test_naive_observed_at_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            ProviderAvailability(available=True, observed_at=NAIVE_NOW)

    def test_does_not_expose_task_execution_states(self) -> None:
        # ProviderAvailability must only ever carry provider-level reasons,
        # never task/execution states like WAITING_RESET/RECOVERY_REQUIRED.
        reason_values = {member.value for member in UnavailabilityReason}
        assert "waiting_reset" not in reason_values
        assert "recovery_required" not in reason_values
        assert "interrupted" not in reason_values


class TestQuotaWindow:
    def test_minimal_window_with_unknown_utilization(self) -> None:
        window = QuotaWindow(
            window_type="primary_5h", source="codex_app_server", observed_at=UTC_NOW
        )
        assert window.utilization is None
        assert window.reset_at is None

    def test_zero_utilization_is_distinct_from_unknown(self) -> None:
        zero = QuotaWindow(
            window_type="five_hour",
            source="claude_stream_json",
            observed_at=UTC_NOW,
            utilization=0.0,
        )
        unknown = QuotaWindow(
            window_type="five_hour",
            source="claude_stream_json",
            observed_at=UTC_NOW,
        )
        assert zero.utilization == 0.0
        assert zero.utilization is not None
        assert unknown.utilization is None

    def test_full_utilization_with_reset_at(self) -> None:
        reset_at = UTC_NOW + timedelta(hours=5)
        window = QuotaWindow(
            window_type="primary_5h",
            source="codex_app_server",
            observed_at=UTC_NOW,
            utilization=1.0,
            reset_at=reset_at,
        )
        assert window.utilization == 1.0
        assert window.reset_at == reset_at

    @pytest.mark.parametrize("bad_utilization", [-0.01, 1.01, 2.0, -1.0])
    def test_utilization_out_of_range_is_rejected(self, bad_utilization: float) -> None:
        with pytest.raises(ValueError, match="fraction in \\[0.0, 1.0\\]"):
            QuotaWindow(
                window_type="primary_5h",
                source="codex_app_server",
                observed_at=UTC_NOW,
                utilization=bad_utilization,
            )

    def test_empty_window_type_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            QuotaWindow(window_type="", source="codex_app_server", observed_at=UTC_NOW)

    def test_empty_source_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            QuotaWindow(window_type="primary_5h", source="", observed_at=UTC_NOW)

    def test_naive_observed_at_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            QuotaWindow(
                window_type="primary_5h", source="codex_app_server", observed_at=NAIVE_NOW
            )

    def test_naive_reset_at_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            QuotaWindow(
                window_type="primary_5h",
                source="codex_app_server",
                observed_at=UTC_NOW,
                reset_at=NAIVE_NOW,
            )


class TestResetCredit:
    def test_available_credit(self) -> None:
        credit = ResetCredit(
            title="Full reset (Weekly + 5 hr)",
            status=ResetCreditStatus.AVAILABLE,
            available_count=1,
        )
        assert credit.status is ResetCreditStatus.AVAILABLE
        assert credit.auto_consume is False

    def test_auto_consume_true_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="auto_consume must be False"):
            ResetCredit(
                title="Full reset (Weekly + 5 hr)",
                status=ResetCreditStatus.AVAILABLE,
                auto_consume=True,
            )

    def test_negative_available_count_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="available_count must be >= 0"):
            ResetCredit(
                title="Full reset (Weekly + 5 hr)",
                status=ResetCreditStatus.AVAILABLE,
                available_count=-1,
            )

    def test_empty_title_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            ResetCredit(title="", status=ResetCreditStatus.AVAILABLE)


class TestProviderState:
    def test_state_with_no_quota_windows(self) -> None:
        state = ProviderState(
            provider="anthropic", availability=_available(), observed_at=UTC_NOW
        )
        assert state.quota_windows == ()
        assert state.reset_credits == ()

    def test_state_with_several_quota_windows(self) -> None:
        five_hour = QuotaWindow(
            window_type="five_hour",
            source="claude_stream_json",
            observed_at=UTC_NOW,
            utilization=0.45,
            reset_at=UTC_NOW + timedelta(hours=5),
        )
        seven_day = QuotaWindow(
            window_type="seven_day",
            source="claude_stream_json",
            observed_at=UTC_NOW,
            utilization=0.23,
            reset_at=UTC_NOW + timedelta(days=7),
        )
        state = ProviderState(
            provider="anthropic",
            availability=_available(),
            observed_at=UTC_NOW,
            quota_windows=(five_hour, seven_day),
        )
        assert len(state.quota_windows) == 2
        assert state.quota_windows[0].window_type == "five_hour"
        assert state.quota_windows[1].window_type == "seven_day"

    def test_state_does_not_assume_exactly_two_windows(self) -> None:
        # A third, arbitrary window label must be accepted without any
        # special-casing — the contract must not assume a fixed count or a
        # fixed set of window identifiers.
        windows = tuple(
            QuotaWindow(
                window_type=label, source="future_provider", observed_at=UTC_NOW
            )
            for label in ("hourly", "daily", "monthly", "concurrent")
        )
        state = ProviderState(
            provider="future_provider",
            availability=_available(),
            observed_at=UTC_NOW,
            quota_windows=windows,
        )
        assert len(state.quota_windows) == 4

    def test_state_accepts_a_list_and_normalizes_to_tuple(self) -> None:
        window = QuotaWindow(
            window_type="primary_5h", source="codex_app_server", observed_at=UTC_NOW
        )
        state = ProviderState(
            provider="openai",
            availability=_available(),
            observed_at=UTC_NOW,
            quota_windows=[window],
        )
        assert isinstance(state.quota_windows, tuple)

    def test_available_provider_representation(self) -> None:
        state = ProviderState(
            provider="anthropic", availability=_available(), observed_at=UTC_NOW
        )
        assert state.availability.available is True
        assert state.availability.reason is None

    def test_unavailable_provider_representation(self) -> None:
        availability = ProviderAvailability(
            available=False,
            observed_at=UTC_NOW,
            reason=UnavailabilityReason.QUOTA_EXHAUSTED,
        )
        state = ProviderState(
            provider="openai", availability=availability, observed_at=UTC_NOW
        )
        assert state.availability.available is False
        assert state.availability.reason is UnavailabilityReason.QUOTA_EXHAUSTED

    def test_unknown_availability_representation(self) -> None:
        availability = ProviderAvailability(
            available=False, observed_at=UTC_NOW, reason=UnavailabilityReason.UNKNOWN
        )
        state = ProviderState(
            provider="ollama", availability=availability, observed_at=UTC_NOW
        )
        assert state.availability.reason is UnavailabilityReason.UNKNOWN

    def test_state_with_reset_credits_present(self) -> None:
        credit = ResetCredit(
            title="Full reset (Weekly + 5 hr)",
            status=ResetCreditStatus.AVAILABLE,
            available_count=1,
        )
        state = ProviderState(
            provider="openai",
            availability=_available(),
            observed_at=UTC_NOW,
            reset_credits=(credit,),
        )
        assert len(state.reset_credits) == 1
        assert state.reset_credits[0].auto_consume is False

    def test_state_with_reset_credits_absent(self) -> None:
        state = ProviderState(
            provider="anthropic", availability=_available(), observed_at=UTC_NOW
        )
        assert state.reset_credits == ()

    def test_empty_provider_name_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            ProviderState(
                provider="", availability=_available(), observed_at=UTC_NOW
            )

    def test_naive_observed_at_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            ProviderState(
                provider="anthropic", availability=_available(), observed_at=NAIVE_NOW
            )

    def test_availability_must_be_a_provider_availability(self) -> None:
        with pytest.raises(TypeError, match="ProviderAvailability"):
            ProviderState(
                provider="anthropic",
                availability="available",  # type: ignore[arg-type]
                observed_at=UTC_NOW,
            )

    def test_quota_windows_reject_wrong_element_type(self) -> None:
        with pytest.raises(TypeError, match="QuotaWindow"):
            ProviderState(
                provider="anthropic",
                availability=_available(),
                observed_at=UTC_NOW,
                quota_windows=["not-a-window"],  # type: ignore[list-item]
            )

    def test_is_immutable(self) -> None:
        state = ProviderState(
            provider="anthropic", availability=_available(), observed_at=UTC_NOW
        )
        with pytest.raises(AttributeError):
            state.provider = "openai"  # type: ignore[misc]
