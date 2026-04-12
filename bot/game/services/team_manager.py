from __future__ import annotations

from telethon import Button
from telethon.events import CallbackQuery, NewMessage
from telethon.tl.types import User
from telethon.utils import get_display_name

from bot.db.models import OwnedPokemon, TeamPreset
from bot.db.repositories import PokemonRepository, TeamRepository, TrainerRepository
from bot.db.session import run_db_work_async
from bot.game.services.pokemon_data import PokemonDataService
from bot.telegram_helpers import safe_event_edit
from bot.game.fusion import effective_species

TEAM_PICKER_PAGE_SIZE = 20


def display_name(user: User | None, fallback: str = "Trainer") -> str:
    if not user:
        return fallback
    value = get_display_name(user).strip()
    return value or fallback


def chunk_buttons(buttons: list[Button], *, per_row: int) -> list[list[Button]]:
    return [buttons[index:index + per_row] for index in range(0, len(buttons), per_row)]


class TeamManagerService:
    def __init__(self, data_service: PokemonDataService) -> None:
        self.data = data_service
        self.pending_rename_team: dict[int, int] = {}

    def _overview_payload(
        self,
        session,
        *,
        owner_id: int,
        username: str | None,
        display_name_value: str,
    ) -> tuple[str, list[list[Button]]]:
        trainers = TrainerRepository(session)
        teams = TeamRepository(session)
        trainer = trainers.ensure_trainer(
            telegram_user_id=owner_id,
            username=username,
            display_name=display_name_value,
        )
        team_list = teams.list_teams(trainer)
        active_team = teams.get_active_team(trainer)
        return (
            self.team_overview_text(active_team, teams),
            self.overview_buttons(team_list),
        )

    def _rename_team_payload(
        self,
        session,
        *,
        owner_id: int,
        username: str | None,
        display_name_value: str,
        team_slot: int,
        new_name: str,
    ) -> dict[str, object]:
        trainers = TrainerRepository(session)
        teams = TeamRepository(session)
        trainer = trainers.ensure_trainer(
            telegram_user_id=owner_id,
            username=username,
            display_name=display_name_value,
        )
        team = teams.get_team(trainer, team_slot)
        if team is None:
            return {"status": "missing"}
        try:
            teams.rename_team(team, new_name)
        except ValueError as exc:
            return {"status": "invalid", "text": str(exc)}
        return {
            "status": "ok",
            "text": self.team_detail_text(team, teams),
            "buttons": self.team_detail_buttons(team),
        }

    def _callback_payload(
        self,
        session,
        *,
        owner_id: int,
        username: str | None,
        display_name_value: str,
        data: str,
    ) -> dict[str, object]:
        trainers = TrainerRepository(session)
        teams = TeamRepository(session)
        pokemons = PokemonRepository(session)
        trainer = trainers.ensure_trainer(
            telegram_user_id=owner_id,
            username=username,
            display_name=display_name_value,
        )
        team_list = teams.list_teams(trainer)
        parts = data.split(":")
        action = parts[1]

        if action == "overview":
            active_team = teams.get_active_team(trainer)
            return {
                "status": "edit",
                "text": self.team_overview_text(active_team, teams),
                "buttons": self.overview_buttons(team_list),
                "answer": None,
            }

        if action == "view" and len(parts) == 3:
            team = teams.get_team(trainer, int(parts[2]))
            if team is None:
                return {"status": "answer", "answer": ("Unknown team.", True)}
            return {
                "status": "edit",
                "text": self.team_detail_text(team, teams),
                "buttons": self.team_detail_buttons(team),
                "answer": None,
            }

        if action == "setmain" and len(parts) == 3:
            selected = teams.set_active_team(trainer, int(parts[2]))
            return {
                "status": "edit",
                "text": self.team_detail_text(selected, teams),
                "buttons": self.team_detail_buttons(selected),
                "answer": (f"{selected.name} is now your active team.", False),
            }

        if action == "rename" and len(parts) == 3:
            team = teams.get_team(trainer, int(parts[2]))
            if team is None:
                return {"status": "answer", "answer": ("Unknown team.", True)}
            return {"status": "rename_prompt", "team_slot": int(team.slot_number)}

        if action == "reset" and len(parts) == 3:
            team = teams.get_team(trainer, int(parts[2]))
            if team is None:
                return {"status": "answer", "answer": ("Unknown team.", True)}
            teams.clear_team(team)
            return {
                "status": "edit",
                "text": self.team_detail_text(team, teams),
                "buttons": self.team_detail_buttons(team),
                "answer": ("Team reset.", False),
            }

        if action == "remove" and len(parts) == 3:
            team = teams.get_team(trainer, int(parts[2]))
            if team is None:
                return {"status": "answer", "answer": ("Unknown team.", True)}
            occupied = self.occupied_slot_indices(team, teams)
            if not occupied:
                return {"status": "answer", "answer": ("That team is empty.", True)}
            return {
                "status": "edit",
                "text": f"{team.name}\nChoose the slot you want to remove.",
                "buttons": self.slot_choice_buttons(team.slot_number, occupied, prefix="removeone"),
                "answer": None,
            }

        if action == "removeone" and len(parts) == 4:
            team = teams.get_team(trainer, int(parts[2]))
            if team is None:
                return {"status": "answer", "answer": ("Unknown team.", True)}
            slot_index = int(parts[3])
            teams.remove_slot(team, slot_index)
            return {
                "status": "edit",
                "text": self.team_detail_text(team, teams),
                "buttons": self.team_detail_buttons(team),
                "answer": (f"Removed slot {slot_index}.", False),
            }

        if action == "swap" and len(parts) == 3:
            team = teams.get_team(trainer, int(parts[2]))
            if team is None:
                return {"status": "answer", "answer": ("Unknown team.", True)}
            occupied = self.occupied_slot_indices(team, teams)
            if len(occupied) < 2:
                return {"status": "answer", "answer": ("You need at least two Pokemon to change order.", True)}
            return {
                "status": "edit",
                "text": f"{team.name}\nChoose the first slot to move.",
                "buttons": self.slot_choice_buttons(team.slot_number, occupied, prefix="swap1"),
                "answer": None,
            }

        if action == "swap1" and len(parts) == 4:
            team = teams.get_team(trainer, int(parts[2]))
            if team is None:
                return {"status": "answer", "answer": ("Unknown team.", True)}
            first_slot = int(parts[3])
            occupied = [slot for slot in self.occupied_slot_indices(team, teams) if slot != first_slot]
            if not occupied:
                return {"status": "answer", "answer": ("Choose a different slot.", True)}
            return {
                "status": "edit",
                "text": f"{team.name}\nChoose the slot to swap with slot {first_slot}.",
                "buttons": self.swap_second_buttons(team.slot_number, first_slot, occupied),
                "answer": None,
            }

        if action == "swap2" and len(parts) == 5:
            team = teams.get_team(trainer, int(parts[2]))
            if team is None:
                return {"status": "answer", "answer": ("Unknown team.", True)}
            teams.swap_slots(team, int(parts[3]), int(parts[4]))
            return {
                "status": "edit",
                "text": self.team_detail_text(team, teams),
                "buttons": self.team_detail_buttons(team),
                "answer": ("Team order updated.", False),
            }

        if action == "add" and len(parts) == 3:
            team = teams.get_team(trainer, int(parts[2]))
            if team is None:
                return {"status": "answer", "answer": ("Unknown team.", True)}
            next_slot = self.next_open_slot(team, teams)
            if next_slot is None:
                return {"status": "answer", "answer": ("That team is already full.", True)}
            page_items, total, page = self.eligible_pokemon_page(
                trainer=trainer,
                pokemons=pokemons,
                teams=teams,
                team=team,
                page=0,
            )
            return {
                "status": "edit",
                "text": self.add_picker_text(team, trainer, page=page, total=total, items=page_items, next_slot=next_slot),
                "buttons": self.add_picker_buttons(team, page=page, total=total, items=page_items),
                "answer": None,
            }

        if action == "addpage" and len(parts) == 4:
            team = teams.get_team(trainer, int(parts[2]))
            if team is None:
                return {"status": "answer", "answer": ("Unknown team.", True)}
            next_slot = self.next_open_slot(team, teams)
            if next_slot is None:
                return {
                    "status": "edit",
                    "text": self.team_detail_text(team, teams),
                    "buttons": self.team_detail_buttons(team),
                    "answer": ("That team is already full.", True),
                }
            page_items, total, page = self.eligible_pokemon_page(
                trainer=trainer,
                pokemons=pokemons,
                teams=teams,
                team=team,
                page=int(parts[3]),
            )
            return {
                "status": "edit",
                "text": self.add_picker_text(team, trainer, page=page, total=total, items=page_items, next_slot=next_slot),
                "buttons": self.add_picker_buttons(team, page=page, total=total, items=page_items),
                "answer": None,
            }

        if action == "addpick" and len(parts) == 5:
            team = teams.get_team(trainer, int(parts[2]))
            if team is None:
                return {"status": "answer", "answer": ("Unknown team.", True)}
            assigned_slot = self.next_open_slot(team, teams)
            if assigned_slot is None:
                return {"status": "answer", "answer": ("That team is already full.", True)}
            pokemon = pokemons.get_owned_pokemon(trainer, int(parts[4]))
            if pokemon is None:
                return {"status": "answer", "answer": ("That Pokemon does not belong to you.", True)}
            if pokemon.id in teams.team_member_ids(team):
                return {"status": "answer", "answer": ("That Pokemon is already in this team.", True)}
            teams.assign_pokemon(team, assigned_slot, pokemon)

            next_slot = self.next_open_slot(team, teams)
            if next_slot is None:
                return {
                    "status": "edit",
                    "text": self.team_detail_text(team, teams),
                    "buttons": self.team_detail_buttons(team),
                    "answer": (f"{pokemon.species} added to slot {assigned_slot}.", False),
                }

            page_items, total, page = self.eligible_pokemon_page(
                trainer=trainer,
                pokemons=pokemons,
                teams=teams,
                team=team,
                page=int(parts[3]),
            )
            if total <= 0:
                return {
                    "status": "edit",
                    "text": self.team_detail_text(team, teams),
                    "buttons": self.team_detail_buttons(team),
                    "answer": (f"{pokemon.species} added to {team.name}.", False),
                }

            return {
                "status": "edit",
                "text": self.add_picker_text(team, trainer, page=page, total=total, items=page_items, next_slot=next_slot),
                "buttons": self.add_picker_buttons(team, page=page, total=total, items=page_items),
                "answer": (f"{pokemon.species} added to slot {assigned_slot}.", False),
            }

        return {"status": "unknown"}

    def occupied_slot_indices(self, team: TeamPreset, teams: TeamRepository) -> list[int]:
        return [slot.slot_index for slot in teams.team_slots(team) if slot.pokemon_id is not None]

    def _format_compact_member(self, index: int, pokemon, *, display_mode: str) -> str | None:
        # If the slot is empty, return None so we can skip it entirely
        if pokemon is None:
            return None

        display_species = effective_species(pokemon)
        shiny_icon = " \u2728" if pokemon.shiny else ""
        suffix = self.data.collection_entry_suffix(pokemon, display_mode) or "-"
        return f"{index}. {display_species}{shiny_icon} - {suffix}"

    async def on_myteam(self, event: NewMessage.Event) -> None:
        sender = await event.get_sender()
        text, buttons = await run_db_work_async(lambda session: self._overview_payload(
            session,
            owner_id=int(event.sender_id or 0),
            username=getattr(sender, "username", None),
            display_name_value=display_name(sender),
        ))
        await event.respond(text, buttons=buttons, parse_mode="md")

    async def on_private_text(self, event: NewMessage.Event) -> bool:
        if not event.is_private or event.raw_text.startswith("/"):
            return False

        team_slot = self.pending_rename_team.pop(event.sender_id, None)
        if team_slot is None:
            return False

        sender = await event.get_sender()
        payload = await run_db_work_async(lambda session: self._rename_team_payload(
            session,
            owner_id=int(event.sender_id or 0),
            username=getattr(sender, "username", None),
            display_name_value=display_name(sender),
            team_slot=int(team_slot),
            new_name=event.raw_text,
        ))
        if payload["status"] == "missing":
            await event.respond("That team no longer exists.")
            return True
        if payload["status"] == "invalid":
            await event.respond(str(payload["text"]))
            return True
        await event.respond(str(payload["text"]), buttons=payload["buttons"], parse_mode="md")
        return True

    async def handle_callback(self, event: CallbackQuery.Event) -> bool:
        data = event.data.decode("utf-8")
        if not data.startswith("team:"):
            return False

        sender = await event.get_sender()
        payload = await run_db_work_async(lambda session: self._callback_payload(
            session,
            owner_id=int(event.sender_id or 0),
            username=getattr(sender, "username", None),
            display_name_value=display_name(sender),
            data=data,
        ))
        status = str(payload.get("status") or "")
        if status == "rename_prompt":
            self.pending_rename_team[event.sender_id] = int(payload["team_slot"])
            await event.answer("Send the new team name in DM.", alert=True)
            return True
        if status == "answer":
            answer_text, alert = payload["answer"]
            await event.answer(str(answer_text), alert=bool(alert))
            return True
        if status == "edit":
            await safe_event_edit(
                event,
                str(payload["text"]),
                buttons=payload["buttons"],
                parse_mode="md",
            )
            answer = payload.get("answer")
            if isinstance(answer, tuple):
                answer_text, alert = answer
                await event.answer(str(answer_text), alert=bool(alert))
            else:
                await event.answer()
            return True

        await event.answer("Unknown team action.", alert=True)
        return True

    def sorted_owned_pokemon(self, trainer, pokemons: PokemonRepository, *, exclude_ids: set[int] | None = None) -> list[OwnedPokemon]:
        pokemon_list = pokemons.list_owned_pokemon(trainer, exclude_ids=exclude_ids)
        return self.data.sort_owned_pokemon(
            pokemon_list,
            sort_mode=trainer.sort_mode,
            descending=trainer.sort_descending,
        )

    def eligible_pokemon_page(
        self,
        *,
        trainer,
        pokemons: PokemonRepository,
        teams: TeamRepository,
        team: TeamPreset,
        page: int,
    ) -> tuple[list[OwnedPokemon], int, int]:
        eligible = self.sorted_owned_pokemon(trainer, pokemons, exclude_ids=teams.team_member_ids(team))
        total = len(eligible)
        if total <= 0:
            return [], 0, 0
        max_page = (total - 1) // TEAM_PICKER_PAGE_SIZE
        current_page = min(max(page, 0), max_page)
        start = current_page * TEAM_PICKER_PAGE_SIZE
        end = start + TEAM_PICKER_PAGE_SIZE
        return eligible[start:end], total, current_page

    def next_open_slot(self, team: TeamPreset, teams: TeamRepository) -> int | None:
        for slot in teams.team_slots(team):
            if slot.pokemon_id is None:
                return int(slot.slot_index)
        return None
    def team_lines(self, team: TeamPreset, teams: TeamRepository) -> list[str]:
        lines: list[str] = []
        display_mode = getattr(getattr(team, "trainer", None), "display_mode", "none")
        for slot in teams.team_slots(team):
            formatted_member = self._format_compact_member(slot.slot_index, slot.pokemon, display_mode=display_mode)
            if formatted_member:
                lines.append(formatted_member)
        return lines if lines else ["Empty team"]

    def team_overview_text(self, active_team: TeamPreset, teams: TeamRepository) -> str:
        active_mark = "\u2705" if active_team.is_active else ""
        lines = [
            f"{active_team.name} : {active_mark}",
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
        ]
        lines.extend(self.team_lines(active_team, teams))
        lines.extend([
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
            "Which team do you want to manage?",
        ])
        return "\n".join(lines)

    def team_detail_text(self, team: TeamPreset, teams: TeamRepository) -> str:
        active_mark = "\u2705" if team.is_active else ""
        lines = [
            f"{team.name} : {active_mark}",
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
        ]
        lines.extend(self.team_lines(team, teams))
        lines.extend([
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
            "Choose an option below to edit this team",
        ])
        return "\n".join(lines)

    def overview_buttons(self, team_list: list[TeamPreset]) -> list[list[Button]]:
        buttons = []
        for team in team_list:
            prefix = "\u2705 " if team.is_active else ""
            label = f"{prefix}{team.name}"
            buttons.append(Button.inline(label[:28], data=f"team:view:{team.slot_number}".encode("utf-8")))
        return chunk_buttons(buttons, per_row=2)

    def team_detail_buttons(self, team: TeamPreset) -> list[list[Button]]:
        main_label = "Main \u2705" if team.is_active else "Set Main"
        return [
            [
                Button.inline("Add Poke", data=f"team:add:{team.slot_number}".encode("utf-8")),
                Button.inline("Remove Poke", data=f"team:remove:{team.slot_number}".encode("utf-8")),
            ],
            [
                Button.inline("Change Order", data=f"team:swap:{team.slot_number}".encode("utf-8")),
                Button.inline("Reset Team", data=f"team:reset:{team.slot_number}".encode("utf-8")),
            ],
            [
                Button.inline(main_label, data=f"team:setmain:{team.slot_number}".encode("utf-8")),
                Button.inline("Rename", data=f"team:rename:{team.slot_number}".encode("utf-8")),
            ],
            [Button.inline("Back", data="team:overview".encode("utf-8"))],
        ]

    def slot_choice_buttons(self, team_slot: int, slot_indices: list[int], *, prefix: str) -> list[list[Button]]:
        buttons = [
            Button.inline(str(slot_index), data=f"team:{prefix}:{team_slot}:{slot_index}".encode("utf-8"))
            for slot_index in slot_indices
        ]
        rows = chunk_buttons(buttons, per_row=3)
        rows.append([Button.inline("Back", data=f"team:view:{team_slot}".encode("utf-8"))])
        return rows

    def swap_second_buttons(self, team_slot: int, first_slot: int, slot_indices: list[int]) -> list[list[Button]]:
        buttons = [
            Button.inline(
                str(slot_index),
                data=f"team:swap2:{team_slot}:{first_slot}:{slot_index}".encode("utf-8"),
            )
            for slot_index in slot_indices
        ]
        rows = chunk_buttons(buttons, per_row=3)
        rows.append([Button.inline("Back", data=f"team:view:{team_slot}".encode("utf-8"))])
        return rows

    def add_picker_text(
        self,
        team: TeamPreset,
        trainer,
        *,
        page: int,
        total: int,
        items: list[OwnedPokemon],
        next_slot: int,
    ) -> str:
        lines = [f"List Of Your Pokes (Page {page + 1}):", ""]
        if not items:
            lines.append("No Pokemon available to add.")
        else:
            start = page * TEAM_PICKER_PAGE_SIZE + 1
            for index, pokemon in enumerate(items, start=start):
                lines.append(f"{index}. {self.data.collection_entry_text(pokemon, trainer.display_mode)}")
        lines.append("")
        lines.append(f"Select Poke To Add In {team.name}")
        lines.append(f"Next Slot : {next_slot}")
        return "\n".join(lines)

    def add_picker_buttons(
        self,
        team: TeamPreset,
        *,
        page: int,
        total: int,
        items: list[OwnedPokemon],
    ) -> list[list[Button]]:
        rows: list[list[Button]] = []
        start = page * TEAM_PICKER_PAGE_SIZE + 1
        number_buttons = [
            Button.inline(str(index), data=f"team:addpick:{team.slot_number}:{page}:{pokemon.id}".encode("utf-8"))
            for index, pokemon in enumerate(items, start=start)
        ]
        rows.extend(chunk_buttons(number_buttons, per_row=5))

        max_page = (max(total, 1) - 1) // TEAM_PICKER_PAGE_SIZE
        nav_row: list[Button] = []
        jump_row: list[Button] = []
        if page > 0:
            nav_row.append(Button.inline("<", data=f"team:addpage:{team.slot_number}:{page - 1}".encode("utf-8")))
            jump_row.append(Button.inline("<<", data=f"team:addpage:{team.slot_number}:0".encode("utf-8")))
        if page < max_page:
            nav_row.append(Button.inline(">", data=f"team:addpage:{team.slot_number}:{page + 1}".encode("utf-8")))
            jump_row.append(Button.inline(">>", data=f"team:addpage:{team.slot_number}:{max_page}".encode("utf-8")))
        if nav_row:
            rows.append(nav_row)
        if jump_row:
            rows.append(jump_row)
        rows.append([Button.inline("Back", data=f"team:view:{team.slot_number}".encode("utf-8"))])
        return rows
