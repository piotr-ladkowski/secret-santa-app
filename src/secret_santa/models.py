from datetime import UTC, datetime

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    # Shown on the join page and included in the assignment email (budget, date, ...).
    message: Mapped[str] = mapped_column(Text, default="")
    # The secret people use to join (in the link or typed on the home page).
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    # Set once, when the draw happens. After that the participant list is frozen.
    drawn_at: Mapped[datetime | None] = mapped_column(default=None)

    participants: Mapped[list["Participant"]] = relationship(
        back_populates="group",
        cascade="all, delete-orphan",
        order_by="Participant.id",  # the order the organizer typed them in
        foreign_keys="Participant.group_id",
    )

    @property
    def is_drawn(self) -> bool:
        return self.drawn_at is not None


class Participant(Base):
    __tablename__ = "participants"
    __table_args__ = (UniqueConstraint("group_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    # NULL until the person picks their name on the join page.
    email: Mapped[str | None] = mapped_column(String(320), default=None)
    registered_at: Mapped[datetime | None] = mapped_column(default=None)
    # Who this person buys a gift for. Never shown in the UI, only sent by email.
    receiver_id: Mapped[int | None] = mapped_column(
        ForeignKey("participants.id", ondelete="SET NULL"), default=None
    )
    email_sent_at: Mapped[datetime | None] = mapped_column(default=None)
    email_error: Mapped[str | None] = mapped_column(Text, default=None)

    group: Mapped[Group] = relationship(back_populates="participants", foreign_keys=[group_id])
    # post_update: the draw links everyone in one circle (A->B->C->A), which SQLAlchemy can only
    # write as a second UPDATE pass.
    receiver: Mapped["Participant | None"] = relationship(
        remote_side=[id], foreign_keys=[receiver_id], post_update=True
    )

    @property
    def is_registered(self) -> bool:
        return self.email is not None
