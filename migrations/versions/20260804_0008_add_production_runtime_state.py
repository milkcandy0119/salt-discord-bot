"""加入試跑結束後的正式運行狀態與封存費用快照。

版本 ID：20260804_0008
上一版本：20260804_0007
建立時間：2026-08-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260804_0008"
down_revision: str | None = "20260804_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """擴充狀態約束；不會自動結束試跑或開啟正式運行。"""

    with op.batch_alter_table("trial_sessions") as batch_op:
        batch_op.drop_constraint("ck_trial_session_status", type_="check")
        batch_op.create_check_constraint(
            "ck_trial_session_status",
            "status IN ('active', 'paused', 'completed', 'stopped', 'production')",
        )
        batch_op.add_column(
            sa.Column("final_global_increment_microusd", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("final_background_increment_microusd", sa.Integer(), nullable=True)
        )


def downgrade() -> None:
    """回復舊狀態集合；正式運行資料會安全降為已完成。"""

    op.execute(
        "UPDATE trial_sessions "
        "SET status = 'completed', stopped_reason = 'downgraded_from_production' "
        "WHERE status = 'production'"
    )
    with op.batch_alter_table("trial_sessions") as batch_op:
        batch_op.drop_column("final_background_increment_microusd")
        batch_op.drop_column("final_global_increment_microusd")
        batch_op.drop_constraint("ck_trial_session_status", type_="check")
        batch_op.create_check_constraint(
            "ck_trial_session_status",
            "status IN ('active', 'paused', 'completed', 'stopped')",
        )
