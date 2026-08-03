from app.security.access_policy import can_view_sensitive_events


def test_only_owner_and_configured_admins_can_view_sensitive_events() -> None:
    admins = frozenset({20, 30})

    assert can_view_sensitive_events(10, owner_user_id=10, admin_user_ids=admins)
    assert can_view_sensitive_events(20, owner_user_id=10, admin_user_ids=admins)
    assert not can_view_sensitive_events(99, owner_user_id=10, admin_user_ids=admins)

