"""敏感事件的存取權限規則。"""


def can_view_sensitive_events(
    requester_user_id: int,
    *,
    owner_user_id: int,
    admin_user_ids: frozenset[int],
) -> bool:
    """判斷使用者是否可以查看敏感資料攔截紀錄。"""

    return requester_user_id == owner_user_id or requester_user_id in admin_user_ids

