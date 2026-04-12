from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class Trainer(Base, TimestampMixin):
    __tablename__ = "trainers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    current_region: Mapped[str] = mapped_column(String(32), default="kanto", nullable=False)
    current_location: Mapped[str | None] = mapped_column(String(96), nullable=True)
    last_safari_entered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    starter_species: Mapped[str | None] = mapped_column(String(64), nullable=True)
    gender: Mapped[str | None] = mapped_column(String(16), nullable=True)
    sort_mode: Mapped[str] = mapped_column(String(16), default="none", nullable=False)
    sort_descending: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    display_mode: Mapped[str] = mapped_column(String(16), default="none", nullable=False)
    challenge_mode: Mapped[str] = mapped_column(String(16), default="owned", nullable=False)
    challenge_generation: Mapped[int] = mapped_column(Integer, default=9, nullable=False)
    battle_visuals: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    trainer_level: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    trainer_exp: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_wins: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_losses: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_caught: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # JSON field for pending move prompts. Stored as either a legacy object or a list of
    # {"id", "pokemon_id", "pokemon_name", "move", "moves", "expires_at", "chat_id", "message_id"} entries.
    pending_move_learning: Mapped[str | None] = mapped_column(Text, nullable=True)
    daycare_state_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    eggs_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    shop_state_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    inventory: Mapped["Inventory"] = relationship(back_populates="trainer", uselist=False, cascade="all, delete-orphan")
    owned_pokemon: Mapped[list["OwnedPokemon"]] = relationship(
        back_populates="trainer",
        cascade="all, delete-orphan",
        order_by="OwnedPokemon.id",
    )
    party_slots: Mapped[list["PartySlot"]] = relationship(
        back_populates="trainer",
        cascade="all, delete-orphan",
        order_by="PartySlot.slot_index",
    )
    team_presets: Mapped[list["TeamPreset"]] = relationship(
        back_populates="trainer",
        cascade="all, delete-orphan",
        order_by="TeamPreset.slot_number",
    )


class Inventory(Base):
    __tablename__ = "inventories"

    trainer_id: Mapped[int] = mapped_column(ForeignKey("trainers.id", ondelete="CASCADE"), primary_key=True)
    victory_points: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    season_points: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    league_points: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    poke_balls: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    great_balls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ultra_balls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    special_balls_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    held_items_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    tm_inventory_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    medicine_inventory_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    key_items_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    egg_energy: Mapped[int] = mapped_column(Integer, default=100, nullable=False)

    trainer: Mapped[Trainer] = relationship(back_populates="inventory")


class BannedUser(Base, TimestampMixin):
    __tablename__ = "banned_users"

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    added_by_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)


class KnownGroupChat(Base, TimestampMixin):
    __tablename__ = "known_group_chats"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)


class CommandLock(Base, TimestampMixin):
    __tablename__ = "command_locks"

    command_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    locked_by_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class RedeemCode(Base, TimestampMixin):
    __tablename__ = "redeem_codes"

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    rewards_json: Mapped[str] = mapped_column(Text, nullable=False)
    max_redemptions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    redeemed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_by_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    claims: Mapped[list["RedeemCodeClaim"]] = relationship(
        back_populates="redeem_code",
        cascade="all, delete-orphan",
        order_by="RedeemCodeClaim.id",
    )


class RedeemCodeClaim(Base, TimestampMixin):
    __tablename__ = "redeem_code_claims"
    __table_args__ = (UniqueConstraint("code", "telegram_user_id", name="uq_redeem_code_claim_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(ForeignKey("redeem_codes.code", ondelete="CASCADE"), nullable=False, index=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    redeem_code: Mapped[RedeemCode] = relationship(back_populates="claims")


class OwnedPokemon(Base, TimestampMixin):
    __tablename__ = "owned_pokemon"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trainer_id: Mapped[int] = mapped_column(ForeignKey("trainers.id", ondelete="CASCADE"), index=True, nullable=False)

    species: Mapped[str] = mapped_column(String(64), nullable=False)
    nickname: Mapped[str | None] = mapped_column(String(64), nullable=True)
    origin_region: Mapped[str] = mapped_column(String(32), default="kanto", nullable=False)
    source_kind: Mapped[str] = mapped_column(String(32), default="starter", nullable=False)

    level: Mapped[int] = mapped_column(Integer, nullable=False)
    experience: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    friendship: Mapped[int] = mapped_column(Integer, default=255, nullable=False)

    ability: Mapped[str] = mapped_column(String(64), nullable=False)
    nature: Mapped[str] = mapped_column(String(32), nullable=False)
    gender: Mapped[str] = mapped_column(String(4), default="", nullable=False)
    item: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    tera_type: Mapped[str] = mapped_column(String(32), default="", nullable=False)

    current_hp: Mapped[int] = mapped_column(Integer, nullable=False)
    max_hp: Mapped[int] = mapped_column(Integer, nullable=False)
    shiny: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    untradeable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    unreleasable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    iv_hp: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    iv_atk: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    iv_def: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    iv_spa: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    iv_spd: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    iv_spe: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    ev_hp: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ev_atk: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ev_def: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ev_spa: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ev_spd: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ev_spe: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    moves_json: Mapped[str] = mapped_column(Text, nullable=False)
    move_history_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    export_text: Mapped[str] = mapped_column(Text, nullable=False)
    packed_set: Mapped[str] = mapped_column(Text, nullable=False)
    form_state_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    trainer: Mapped[Trainer] = relationship(back_populates="owned_pokemon")
    party_slots: Mapped[list["PartySlot"]] = relationship(back_populates="pokemon")
    team_slots: Mapped[list["TeamPresetSlot"]] = relationship(back_populates="pokemon")

    @property
    def total_iv(self) -> int:
        return self.iv_hp + self.iv_atk + self.iv_def + self.iv_spa + self.iv_spd + self.iv_spe


class PartySlot(Base, TimestampMixin):
    __tablename__ = "party_slots"
    __table_args__ = (UniqueConstraint("trainer_id", "slot_index", name="uq_party_slot"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trainer_id: Mapped[int] = mapped_column(ForeignKey("trainers.id", ondelete="CASCADE"), nullable=False, index=True)
    slot_index: Mapped[int] = mapped_column(Integer, nullable=False)
    pokemon_id: Mapped[int | None] = mapped_column(ForeignKey("owned_pokemon.id", ondelete="SET NULL"), nullable=True)

    trainer: Mapped[Trainer] = relationship(back_populates="party_slots")
    pokemon: Mapped[OwnedPokemon | None] = relationship(back_populates="party_slots")


class TeamPreset(Base, TimestampMixin):
    __tablename__ = "team_presets"
    __table_args__ = (UniqueConstraint("trainer_id", "slot_number", name="uq_team_preset"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trainer_id: Mapped[int] = mapped_column(ForeignKey("trainers.id", ondelete="CASCADE"), nullable=False, index=True)
    slot_number: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    trainer: Mapped[Trainer] = relationship(back_populates="team_presets")
    slots: Mapped[list["TeamPresetSlot"]] = relationship(
        back_populates="team",
        cascade="all, delete-orphan",
        order_by="TeamPresetSlot.slot_index",
    )


class TeamPresetSlot(Base, TimestampMixin):
    __tablename__ = "team_preset_slots"
    __table_args__ = (UniqueConstraint("team_id", "slot_index", name="uq_team_preset_slot"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("team_presets.id", ondelete="CASCADE"), nullable=False, index=True)
    slot_index: Mapped[int] = mapped_column(Integer, nullable=False)
    pokemon_id: Mapped[int | None] = mapped_column(ForeignKey("owned_pokemon.id", ondelete="SET NULL"), nullable=True)

    team: Mapped[TeamPreset] = relationship(back_populates="slots")
    pokemon: Mapped[OwnedPokemon | None] = relationship(back_populates="team_slots")
