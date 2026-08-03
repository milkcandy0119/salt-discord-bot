"""建立階段 5 背景摘要、向量與持久化佇列。

版本 ID：20260804_0004
上一版本：20260803_0003
建立時間：2026-08-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260804_0004"
down_revision: str | None = "20260803_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """建立可重建摘要、BLOB 向量索引及可恢復背景工作。"""

    op.create_table(
        "segment_summaries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("segment_id", sa.Integer(), nullable=False),
        sa.Column("source_through_message_record_id", sa.Integer(), nullable=False),
        sa.Column("source_message_count", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("provider_response_id", sa.String(length=128), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("source_message_count > 0", name="ck_summaries_message_count"),
        sa.CheckConstraint("input_tokens >= 0", name="ck_summaries_input_tokens"),
        sa.CheckConstraint("output_tokens >= 0", name="ck_summaries_output_tokens"),
        sa.ForeignKeyConstraint(
            ["segment_id"], ["conversation_segments.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "segment_id",
            "source_through_message_record_id",
            "model_name",
            "prompt_version",
            name="uq_segment_summary_source_version",
        ),
    )
    op.create_index("ix_segment_summaries_segment_id", "segment_summaries", ["segment_id"])

    op.create_table(
        "summary_embeddings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("summary_id", sa.Integer(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("chunk_text", sa.Text(), nullable=False),
        sa.Column("source_text_sha256", sa.String(length=64), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("vector_blob", sa.LargeBinary(), nullable=False),
        sa.Column("vector_norm", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("chunk_index >= 0", name="ck_embeddings_chunk_index"),
        sa.CheckConstraint("dimension > 0", name="ck_embeddings_dimension"),
        sa.CheckConstraint("vector_norm > 0", name="ck_embeddings_vector_norm"),
        sa.ForeignKeyConstraint(
            ["summary_id"], ["segment_summaries.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "summary_id",
            "chunk_index",
            "model_name",
            "dimension",
            name="uq_summary_embedding_chunk_model",
        ),
    )
    op.create_index("ix_summary_embeddings_summary_id", "summary_embeddings", ["summary_id"])

    op.create_table(
        "background_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_type", sa.String(length=32), nullable=False),
        sa.Column("segment_id", sa.Integer(), nullable=True),
        sa.Column("summary_id", sa.Integer(), nullable=True),
        sa.Column("source_through_message_record_id", sa.Integer(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("attempts >= 0", name="ck_background_jobs_attempts"),
        sa.CheckConstraint("max_attempts > 0", name="ck_background_jobs_max_attempts"),
        sa.ForeignKeyConstraint(
            ["segment_id"], ["conversation_segments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["summary_id"], ["segment_summaries.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("ix_background_jobs_job_type", "background_jobs", ["job_type"])
    op.create_index("ix_background_jobs_segment_id", "background_jobs", ["segment_id"])
    op.create_index("ix_background_jobs_summary_id", "background_jobs", ["summary_id"])
    op.create_index("ix_background_jobs_status", "background_jobs", ["status"])
    op.create_index("ix_background_jobs_available_at", "background_jobs", ["available_at"])
    op.create_index(
        "ix_background_jobs_ready_order",
        "background_jobs",
        ["status", "available_at", "created_at", "id"],
    )


def downgrade() -> None:
    """移除階段 5 衍生資料與背景工作。"""

    op.drop_index("ix_background_jobs_ready_order", table_name="background_jobs")
    op.drop_index("ix_background_jobs_available_at", table_name="background_jobs")
    op.drop_index("ix_background_jobs_status", table_name="background_jobs")
    op.drop_index("ix_background_jobs_summary_id", table_name="background_jobs")
    op.drop_index("ix_background_jobs_segment_id", table_name="background_jobs")
    op.drop_index("ix_background_jobs_job_type", table_name="background_jobs")
    op.drop_table("background_jobs")
    op.drop_index("ix_summary_embeddings_summary_id", table_name="summary_embeddings")
    op.drop_table("summary_embeddings")
    op.drop_index("ix_segment_summaries_segment_id", table_name="segment_summaries")
    op.drop_table("segment_summaries")
