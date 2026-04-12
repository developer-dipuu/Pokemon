from __future__ import annotations
import asyncio
import json
from typing import Any

from telethon import Button
from telethon.events import CallbackQuery, NewMessage
from telethon.tl.types import User
from telethon.utils import get_display_name

from bot.bridge.showdown_bridge import ShowdownBridgeError
from bot.db.models import OwnedPokemon
from bot.db.repositories import InventoryRepository, PokemonRepository, TrainerRepository, pokemon_display_label
from bot.db.session import run_db_work_async
from bot.game.fusion import effective_species, has_form_state, lookup_species_name
from bot.game.services.generator import PokemonGeneratorService
from bot.game.services.pokemon_data import PokemonDataService
from bot.telegram_helpers import resolve_event_user, safe_event_edit
import difflib

def display_name(user: User | None, fallback: str = "Trainer") -> str:
    if not user:
        return fallback
    value = get_display_name(user).strip()
    return value or fallback


class PokemonStatsService:
    def __init__(
        self,
        data_service: PokemonDataService,
        generator: PokemonGeneratorService | None = None,
        battle_service: Any | None = None,
    ) -> None:
        self.data = data_service
        self.generator = generator
        self.battle_service = battle_service
        self.encounter_service = None

    def attach_encounter_service(self, encounter_service: Any) -> None:
        self.encounter_service = encounter_service

    async def on_stats(self, event: NewMessage.Event) -> None:
        query = event.raw_text.split(maxsplit=1)[1].strip() if len(event.raw_text.split(maxsplit=1)) > 1 else ""
        if not query:
            await event.respond("Use /stats pokemonname.")
            return

        sender = await resolve_event_user(event)
        result = await run_db_work_async(lambda session: self._load_stats_query_result(
            session,
            owner_id=int(event.sender_id or 0),
            username=getattr(sender, "username", None),
            display_name_value=display_name(sender),
            query=query,
        ))
        response_text = result["response_text"]
        response_buttons = result["response_buttons"]
        suggested_query = str(result.get("suggested_query") or "").strip()
        selected = result["selected"]
        selected_region_id = result["selected_region_id"]
        if response_text is not None:
            if suggested_query:
                response_buttons = [
                    [
                        Button.inline(
                            "Yes",
                            data=f"pstats:suggestyes:{int(event.sender_id or 0)}:{suggested_query}".encode("utf-8"),
                        ),
                        Button.inline(
                            "No",
                            data=f"pstats:suggestno:{int(event.sender_id or 0)}".encode("utf-8"),
                        ),
                    ]
                ]
            await self._respond_with_reply(event, response_text, buttons=response_buttons)
            return
        if selected is not None:
            await self.send_stats_card(event, selected, page="summary", region_id=selected_region_id)
            return

    def _load_stats_query_result(
        self,
        session,
        *,
        owner_id: int,
        username: str | None,
        display_name_value: str,
        query: str,
    ) -> dict[str, Any]:
        selected: OwnedPokemon | None = None
        selected_region_id: str | None = None
        response_text: str | None = None
        response_buttons = None
        trainers = TrainerRepository(session)
        pokemons = PokemonRepository(session)
        trainer = trainers.ensure_trainer(
            telegram_user_id=owner_id,
            username=username,
            display_name=display_name_value,
        )
        matches = pokemons.find_by_query(trainer, query)
        suggested_query = ""
        
        if not matches:
            # --- NEW FUZZY MATCHING LOGIC ---
            all_pokemon = pokemons.list_owned_pokemon(trainer)
            all_names = list({p.species for p in all_pokemon} | {p.nickname for p in all_pokemon if p.nickname})
            close_matches = difflib.get_close_matches(query, all_names, n=3, cutoff=0.5)
            
            if close_matches:
                suggested_query = str(close_matches[0]).strip()
                response_text = f"No owned Pokemon matched '{query}'.\nDid you mean **{suggested_query}**?"
            else:
                response_text = f"No owned Pokemon matched '{query}'."
            # --------------------------------
            
        elif len(matches) == 1:
            selected = matches[0]
            selected_region_id = str(trainer.current_region or "").strip() or None
            session.expunge(selected)
        else:
            response_text = self.selection_text(query, trainer.display_mode, matches)
            response_buttons = self.selection_buttons(matches, owner_id=owner_id)
            
        return {
            "selected": selected,
            "selected_region_id": selected_region_id,
            "response_text": response_text,
            "response_buttons": response_buttons,
            "suggested_query": suggested_query,
        }

    async def handle_callback(self, event: CallbackQuery.Event) -> bool:
        data = event.data.decode("utf-8")
        if not data.startswith("pstats:"):
            return False

        parts = data.split(":")
        action = parts[1]

        owner_id, payload = await self._parse_owner_payload(event, action=action, parts=parts)
        if owner_id is None:
            return True

        if action == "pick" and len(payload) == 1:
            pokemon_id = int(payload[0])
            pokemon, region_id = await self.fetch_owned_context(event, owner_id, pokemon_id)
            if pokemon is None:
                await event.answer("That Pokemon is no longer available.", alert=True)
                return True

            try:
                await event.delete()
            except Exception:
                pass
            await self.send_stats_card(event, pokemon, page="summary", owner_id=owner_id, region_id=region_id)
            await event.answer()
            return True

        if action == "suggestyes" and len(payload) == 1:
            suggested_query = str(payload[0] or "").strip()
            if not suggested_query:
                await event.answer("That suggestion expired.", alert=True)
                return True
            sender = await resolve_event_user(event)
            result = await run_db_work_async(lambda session: self._load_stats_query_result(
                session,
                owner_id=owner_id,
                username=getattr(sender, "username", None),
                display_name_value=display_name(sender),
                query=suggested_query,
            ))
            selected = result["selected"]
            selected_region_id = result["selected_region_id"]
            response_text = result["response_text"]
            response_buttons = result["response_buttons"]
            if selected is not None:
                try:
                    await event.delete()
                except Exception:
                    pass
                await self.send_stats_card(event, selected, page="summary", owner_id=owner_id, region_id=selected_region_id)
                await event.answer()
                return True
            if response_text is not None:
                await safe_event_edit(event, response_text, buttons=response_buttons)
                await event.answer()
                return True
            await event.answer("No matching Pokemon found.", alert=True)
            return True

        if action == "suggestno":
            await safe_event_edit(event, "No problem. Use /stats <pokemon> anytime.", buttons=None)
            await event.answer()
            return True

        if action == "page" and len(payload) == 2:
            pokemon, region_id = await self.fetch_owned_context(event, owner_id, int(payload[0]))
            if pokemon is None:
                await event.answer("That Pokemon is no longer available.", alert=True)
                return True
            await self.edit_stats_card(event, pokemon, page=payload[1], owner_id=owner_id, region_id=region_id)
            await event.answer()
            return True

        if action == "evolve" and len(payload) == 2:
            await self.evolve_pokemon(event, int(payload[0]), payload[1], owner_id=owner_id)
            return True

        if action == "removeitem" and len(payload) == 1:
            await self.remove_held_item(event, int(payload[0]), owner_id=owner_id)
            return True

        if action == "release" and len(payload) == 1:
            pokemon, _region_id = await self.fetch_owned_context(event, owner_id, int(payload[0]))
            if pokemon is None:
                await event.answer("That Pokemon is no longer available.", alert=True)
                return True
            if has_form_state(pokemon):
                await event.answer("Unfuse or reset this Pokemon's form before releasing it.", alert=True)
                return True
            await safe_event_edit(
                event,
                f"Release {effective_species(pokemon)}?\nThis cannot be undone.",
                buttons=[
                    [
                        Button.inline("Confirm Release", data=f"pstats:releaseconfirm:{owner_id}:{pokemon.id}".encode("utf-8")),
                        Button.inline("Back", data=f"pstats:page:{owner_id}:{pokemon.id}:summary".encode("utf-8")),
                    ]
                ],
            )
            await event.answer()
            return True

        if action == "releaseconfirm" and len(payload) == 1:
            sender = await resolve_event_user(event)
            result = await run_db_work_async(lambda session: self._release_owned_pokemon(
                session,
                owner_id=owner_id,
                pokemon_id=int(payload[0]),
                username=getattr(sender, "username", None),
                display_name_value=display_name(sender),
            ))
            status = result["status"]
            if status == "missing":
                await event.answer("That Pokemon is no longer available.", alert=True)
                return True
            if status == "form_locked":
                await event.answer("Unfuse or reset this Pokemon's form before releasing it.", alert=True)
                return True
            if status == "unreleasable":
                await event.answer("This Pokemon cannot be released.", alert=True)
                return True
            result_text = result["text"]
            if result_text is not None:
                await safe_event_edit(event, result_text, buttons=None)
                await event.answer("Pokemon released.")
                return True

        await event.answer("Unknown stats action.", alert=True)
        return True

    async def _parse_owner_payload(
        self,
        event: CallbackQuery.Event,
        *,
        action: str,
        parts: list[str],
    ) -> tuple[int | None, list[str]]:
        required_len = 3 if action == "suggestno" else 4
        if len(parts) < required_len:
            await event.answer("That stats panel is stale. Run /stats again.", alert=True)
            return None, []
        if not parts[2].isdigit():
            await event.answer("That stats panel is invalid.", alert=True)
            return None, []
        owner_id = int(parts[2])
        if int(event.sender_id or 0) != owner_id:
            await event.answer("That stats panel belongs to another trainer.", alert=True)
            return None, []
        return owner_id, parts[3:]

    async def fetch_owned_context(
        self,
        event: CallbackQuery.Event,
        owner_id: int,
        pokemon_id: int,
    ) -> tuple[OwnedPokemon | None, str | None]:
        sender = await resolve_event_user(event)
        return await run_db_work_async(lambda session: self._fetch_owned_context_sync(
            session,
            owner_id=owner_id,
            pokemon_id=pokemon_id,
            username=getattr(sender, "username", None),
            display_name_value=display_name(sender),
        ))

    def _fetch_owned_context_sync(
        self,
        session,
        *,
        owner_id: int,
        pokemon_id: int,
        username: str | None,
        display_name_value: str,
    ) -> tuple[OwnedPokemon | None, str | None]:
        trainers = TrainerRepository(session)
        pokemons = PokemonRepository(session)
        trainer = trainers.ensure_trainer(
            telegram_user_id=owner_id,
            username=username,
            display_name=display_name_value,
        )
        pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
        if pokemon is None:
            return None, str(trainer.current_region or "").strip() or None
        region_id = str(trainer.current_region or "").strip() or None
        session.expunge(pokemon)
        return pokemon, region_id

    def selection_text(self, query: str, display_mode: str, matches: list[OwnedPokemon]) -> str:
        lines = [f"Check stats of '{query}'", ""]
        for index, pokemon in enumerate(matches[:12], start=1):
            lines.append(f"{index}. {pokemon_display_label(pokemon, display_mode)}")
        return "\n".join(lines)

    def selection_buttons(self, matches: list[OwnedPokemon], *, owner_id: int) -> list[list[Button]]:
        buttons = [
            Button.inline(str(index), data=f"pstats:pick:{owner_id}:{pokemon.id}".encode("utf-8"))
            for index, pokemon in enumerate(matches[:12], start=1)
        ]
        return [buttons[index:index + 4] for index in range(0, len(buttons), 4)]

    def evolution_buttons(
        self,
        pokemon: OwnedPokemon,
        *,
        owner_id: int,
        region_id: str | None = None,
    ) -> list[list[Button]]:
        choices = self.data.eligible_evolution_choices(pokemon, region_id=region_id)
        if not choices:
            return []
        buttons = [
            Button.inline(
                str(choice["species"]),
                data=f"pstats:evolve:{owner_id}:{pokemon.id}:{choice['target_key']}".encode("utf-8"),
            )
            for choice in choices
        ]
        return [buttons[index:index + 4] for index in range(0, len(buttons), 4)]

    def stats_buttons(
        self,
        pokemon: OwnedPokemon,
        current_page: str = "summary",
        *,
        owner_id: int,
        region_id: str | None = None,
    ) -> list[list[Button]]:
        tabs = [
            ("ivevs", "IV/EVs"),
            ("moves", "Moves"),
            ("item", "Item"),
            ("evolve", "Evolve"),
            ("stats", "Stats"),
        ]

        nav_buttons: list[Button] = []
        for page_id, label in tabs:
            if page_id == current_page:
                nav_buttons.append(Button.inline("Main", data=f"pstats:page:{owner_id}:{pokemon.id}:summary".encode("utf-8")))
            else:
                nav_buttons.append(Button.inline(label, data=f"pstats:page:{owner_id}:{pokemon.id}:{page_id}".encode("utf-8")))

        release_btn = Button.inline("Release", data=f"pstats:release:{owner_id}:{pokemon.id}".encode("utf-8"))
        rows = [
            nav_buttons[0:3],
            nav_buttons[3:5] + [release_btn],
        ]
        if current_page == "item" and pokemon.item:
            rows.append([Button.inline("Remove Held Item", data=f"pstats:removeitem:{owner_id}:{pokemon.id}".encode("utf-8"))])
        if current_page == "evolve":
            rows.extend(self.evolution_buttons(pokemon, owner_id=owner_id, region_id=region_id))
        return rows

    def page_text(self, pokemon: OwnedPokemon, page: str, *, region_id: str | None = None) -> str:
        if page == "ivevs":
            return self.data.iv_evs_text(pokemon)
        if page == "moves":
            return self.data.moves_page_text(pokemon)
        if page == "item":
            return self.data.item_page_text(pokemon)
        if page == "evolve":
            return self.data.evolution_page_text(pokemon, region_id=region_id)
        if page == "stats":
            return self.data.stats_page_text(pokemon)
        return self.data.summary_text(pokemon)

    async def _current_region(self, event: NewMessage.Event | CallbackQuery.Event) -> str | None:
        sender = await resolve_event_user(event)
        return await run_db_work_async(lambda session: self._current_region_sync(
            session,
            owner_id=int(event.sender_id or 0),
            username=getattr(sender, "username", None),
            display_name_value=display_name(sender),
        ))

    def _current_region_sync(
        self,
        session,
        *,
        owner_id: int,
        username: str | None,
        display_name_value: str,
    ) -> str | None:
        trainers = TrainerRepository(session)
        trainer = trainers.ensure_trainer(
            telegram_user_id=owner_id,
            username=username,
            display_name=display_name_value,
        )
        return str(trainer.current_region or "").strip() or None

    def _busy_reason(self, user_id: int) -> str | None:
        if self.battle_service is not None:
            reason = self.battle_service.pvp_lock_reason(user_id)
            if reason:
                return reason
            reason = self.battle_service.encounter_lock_reason(user_id)
            if reason:
                return reason
        if self.encounter_service is not None and self.encounter_service.active_by_user.get(user_id) is not None:
            return "Finish your current encounter before evolving a Pokemon."
        return None

    def _group_reply_target(self, event: NewMessage.Event | CallbackQuery.Event) -> int | None:
        if not isinstance(event, NewMessage.Event):
            return None
        if event.is_private:
            return None
        message = getattr(event, "message", None)
        return getattr(message, "id", None) or getattr(event, "id", None)

    def _should_include_artwork(self, event: NewMessage.Event | CallbackQuery.Event) -> bool:
        return True

    def _artwork_candidates_for_pokemon(self, pokemon: OwnedPokemon) -> list[str]:
        display_species = str(effective_species(pokemon) or "").strip()
        species_names: list[str] = []
        for value in (display_species, lookup_species_name(display_species)):
            candidate = str(value or "").strip()
            if candidate and candidate not in species_names:
                species_names.append(candidate)

        candidates: list[str] = []
        seen: set[str] = set()
        for species_name in species_names:
            for artwork in self.data.artwork_candidates(species_name, shiny=pokemon.shiny):
                if artwork and artwork not in seen:
                    seen.add(artwork)
                    candidates.append(artwork)
        return candidates

    async def _respond_with_reply(
        self,
        event: NewMessage.Event | CallbackQuery.Event,
        text: str,
        *,
        buttons=None,
        parse_mode: str | None = None,
        file=None,
    ) -> None:
        kwargs: dict[str, Any] = {"buttons": buttons}
        if parse_mode is not None:
            kwargs["parse_mode"] = parse_mode
        if file is not None:
            kwargs["file"] = file

        reply_to = self._group_reply_target(event)
        if reply_to is not None:
            try:
                await event.respond(text, reply_to=reply_to, **kwargs)
                return
            except Exception:
                pass
        await event.respond(text, **kwargs)

    async def send_stats_card(
        self,
        event: NewMessage.Event | CallbackQuery.Event,
        pokemon: OwnedPokemon,
        *,
        page: str,
        owner_id: int | None = None,
        region_id: str | None = None,
    ) -> None:
        if region_id is None:
            region_id = await self._current_region(event)
        target_owner_id = int(owner_id or event.sender_id or 0)
        text = self.page_text(pokemon, page, region_id=region_id)
        buttons = self.stats_buttons(pokemon, page, owner_id=target_owner_id, region_id=region_id)
        if self._should_include_artwork(event):
            for candidate in self._artwork_candidates_for_pokemon(pokemon):
                try:
                    await self._respond_with_reply(event, text, file=candidate, buttons=buttons, parse_mode="md")
                    return
                except Exception:
                    continue
        await self._respond_with_reply(event, text, buttons=buttons, parse_mode="md")

    async def edit_stats_card(
        self,
        event: CallbackQuery.Event,
        pokemon: OwnedPokemon,
        *,
        page: str,
        owner_id: int | None = None,
        region_id: str | None = None,
    ) -> None:
        if region_id is None:
            region_id = await self._current_region(event)
        target_owner_id = int(owner_id or event.sender_id or 0)
        await safe_event_edit(
            event,
            self.page_text(pokemon, page, region_id=region_id),
            buttons=self.stats_buttons(pokemon, page, owner_id=target_owner_id, region_id=region_id),
            parse_mode="md",
        )

    async def send_evolution_result(
        self,
        event: CallbackQuery.Event,
        pokemon: OwnedPokemon,
        *,
        previous_species: str,
        owner_id: int | None = None,
    ) -> None:
        text = f"{previous_species} evolved into {effective_species(pokemon)}!"
        target_owner_id = int(owner_id or event.sender_id or 0)
        buttons = self.stats_buttons(pokemon, "summary", owner_id=target_owner_id)
        if self._should_include_artwork(event):
            for candidate in self._artwork_candidates_for_pokemon(pokemon):
                try:
                    await event.respond(text, file=candidate, buttons=buttons)
                    return
                except Exception:
                    continue
        await event.respond(text, buttons=buttons)

    async def evolve_pokemon(self, event: CallbackQuery.Event, pokemon_id: int, target_key: str, *, owner_id: int) -> None:
        if self.generator is None:
            await event.answer("Evolution is not configured right now.", alert=True)
            return

        busy_reason = self._busy_reason(event.sender_id)
        if busy_reason:
            await event.answer(busy_reason, alert=True)
            return

        sender = await resolve_event_user(event)
        evolved_pokemon: OwnedPokemon | None = None
        previous_species = ""

        try:
            prep = await run_db_work_async(lambda session: self._prepare_evolution(
                session,
                owner_id=owner_id,
                pokemon_id=pokemon_id,
                target_key=target_key,
                username=getattr(sender, "username", None),
                display_name_value=display_name(sender),
            ))
            status = prep["status"]
            if status == "missing":
                await event.answer("That Pokemon is no longer available.", alert=True)
                return
            if status == "form_locked":
                await event.answer("Unfuse or reset this Pokemon's form before evolving it.", alert=True)
                return
            if status == "choice_missing":
                await event.answer("That evolution path is no longer available.", alert=True)
                return
            if status == "not_ready":
                await event.answer(str(prep["status_text"] or "That Pokemon is not ready to evolve."), alert=True)
                return

            previous_species = str(prep["previous_species"] or "")
            generated = await self.generator.generate_pokemon(**prep["generator_kwargs"])
            evolved_pokemon = await run_db_work_async(lambda session: self._apply_evolution(
                session,
                owner_id=owner_id,
                pokemon_id=pokemon_id,
                generated=generated,
                username=getattr(sender, "username", None),
                display_name_value=display_name(sender),
            ))
        except ShowdownBridgeError as exc:
            await event.answer(str(exc), alert=True)
            return

        if evolved_pokemon is None:
            await event.answer("Evolution failed.", alert=True)
            return

        await safe_event_edit(event, f"{previous_species} is evolving...", buttons=None)
        await asyncio.sleep(1)
        try:
            await event.delete()
        except Exception:
            pass
        await self.send_evolution_result(event, evolved_pokemon, previous_species=previous_species, owner_id=owner_id)
        await event.answer(f"{evolved_pokemon.species} joined the team.")

    def _prepare_evolution(
        self,
        session,
        *,
        owner_id: int,
        pokemon_id: int,
        target_key: str,
        username: str | None,
        display_name_value: str,
    ) -> dict[str, Any]:
        trainers = TrainerRepository(session)
        pokemons = PokemonRepository(session)
        trainer = trainers.ensure_trainer(
            telegram_user_id=owner_id,
            username=username,
            display_name=display_name_value,
        )
        pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
        if pokemon is None:
            return {"status": "missing"}
        if has_form_state(pokemon):
            return {"status": "form_locked"}

        choice = self.data.evolution_choice(pokemon, target_key, region_id=trainer.current_region)
        if choice is None:
            return {"status": "choice_missing"}
        if not bool(choice.get("ready")):
            return {"status": "not_ready", "status_text": str(choice.get("status_text") or "")}

        return {
            "status": "ok",
            "previous_species": pokemon.species,
            "generator_kwargs": {
                "species": str(choice["species"]),
                "level": int(pokemon.level),
                "region": str(pokemon.origin_region),
                "source_kind": str(pokemon.source_kind),
                "friendship": int(pokemon.friendship),
                "shiny": bool(pokemon.shiny),
                "item": str(pokemon.item or ""),
                "untradeable": bool(pokemon.untradeable),
                "unreleasable": bool(pokemon.unreleasable),
                "ivs": {
                    "hp": int(pokemon.iv_hp),
                    "atk": int(pokemon.iv_atk),
                    "def": int(pokemon.iv_def),
                    "spa": int(pokemon.iv_spa),
                    "spd": int(pokemon.iv_spd),
                    "spe": int(pokemon.iv_spe),
                },
                "evs": {
                    "hp": int(pokemon.ev_hp),
                    "atk": int(pokemon.ev_atk),
                    "def": int(pokemon.ev_def),
                    "spa": int(pokemon.ev_spa),
                    "spd": int(pokemon.ev_spd),
                    "spe": int(pokemon.ev_spe),
                },
                "moves": list(json.loads(pokemon.moves_json)),
                "nature": str(pokemon.nature),
                "ability": str(pokemon.ability),
                "gender": str(pokemon.gender or ""),
                "tera_type": str(pokemon.tera_type or ""),
            },
        }

    def _apply_evolution(
        self,
        session,
        *,
        owner_id: int,
        pokemon_id: int,
        generated: dict[str, Any],
        username: str | None,
        display_name_value: str,
    ) -> OwnedPokemon | None:
        trainers = TrainerRepository(session)
        pokemons = PokemonRepository(session)
        trainer = trainers.ensure_trainer(
            telegram_user_id=owner_id,
            username=username,
            display_name=display_name_value,
        )
        pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
        if pokemon is None or has_form_state(pokemon):
            return None
        pokemons.evolve_owned_pokemon(pokemon, generated)
        session.expunge(pokemon)
        return pokemon

    async def remove_held_item(self, event: CallbackQuery.Event, pokemon_id: int, *, owner_id: int) -> None:
        busy_reason = self._busy_reason(event.sender_id)
        if busy_reason:
            await event.answer(busy_reason, alert=True)
            return

        sender = await resolve_event_user(event)
        result = await run_db_work_async(lambda session: self._remove_held_item_sync(
            session,
            owner_id=owner_id,
            pokemon_id=pokemon_id,
            username=getattr(sender, "username", None),
            display_name_value=display_name(sender),
        ))
        status = result["status"]
        if status == "missing":
            await event.answer("That Pokemon is no longer available.", alert=True)
            return
        if status == "no_item":
            await event.answer("That Pokemon is not holding an item.", alert=True)
            return

        updated_pokemon = result["pokemon"]
        removed_item = str(result["removed_item"] or "")
        region_id = result["region_id"]
        if updated_pokemon is None:
            await event.answer("Could not remove the held item.", alert=True)
            return

        await self.edit_stats_card(event, updated_pokemon, page="item", owner_id=owner_id, region_id=region_id)
        await event.answer(f"Removed {removed_item}.")

    def _remove_held_item_sync(
        self,
        session,
        *,
        owner_id: int,
        pokemon_id: int,
        username: str | None,
        display_name_value: str,
    ) -> dict[str, Any]:
        trainers = TrainerRepository(session)
        inventories = InventoryRepository(session)
        pokemons = PokemonRepository(session)
        trainer = trainers.ensure_trainer(
            telegram_user_id=owner_id,
            username=username,
            display_name=display_name_value,
        )
        region_id = str(trainer.current_region or "").strip() or None
        pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
        if pokemon is None:
            return {"status": "missing"}
        removed_item = str(pokemon.item or "").strip()
        if not removed_item:
            return {"status": "no_item"}

        inventories.add_item(trainer, removed_item)
        pokemon.item = ""
        pokemons.sync_packed_set(pokemon, self.data)
        session.expunge(pokemon)
        return {
            "status": "ok",
            "pokemon": pokemon,
            "removed_item": removed_item,
            "region_id": region_id,
        }

    def _release_owned_pokemon(
        self,
        session,
        *,
        owner_id: int,
        pokemon_id: int,
        username: str | None,
        display_name_value: str,
    ) -> dict[str, Any]:
        trainers = TrainerRepository(session)
        pokemons = PokemonRepository(session)
        trainer = trainers.ensure_trainer(
            telegram_user_id=owner_id,
            username=username,
            display_name=display_name_value,
        )
        pokemon = pokemons.get_owned_pokemon(trainer, pokemon_id)
        if pokemon is None:
            return {"status": "missing"}
        if has_form_state(pokemon):
            return {"status": "form_locked"}
        if pokemon.unreleasable:
            return {"status": "unreleasable"}
        species = pokemon.species
        pokemons.delete_owned_pokemon(pokemon)
        return {"status": "ok", "text": f"{species} was released."}
