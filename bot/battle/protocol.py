from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


STATUS_MAP = {
    "brn": "BRN",
    "frz": "FRZ",
    "par": "PAR",
    "psn": "PSN",
    "slp": "SLP",
    "tox": "TOX",
}

STAT_NAMES = {
    "atk": "Attack",
    "def": "Defense",
    "spa": "Sp. Atk",
    "spd": "Sp. Def",
    "spe": "Speed",
    "accuracy": "Accuracy",
    "evasion": "Evasion",
}


def protocol_parts(line: str) -> tuple[str, list[str]]:
    pieces = line.split("|")
    if len(pieces) < 2:
        return "", []
    return pieces[1], pieces[2:]


def ident_side(ident: str) -> str:
    match = re.match(r"^(p[1-4])", ident)
    return match.group(1) if match else ""


def ident_name(ident: str) -> str:
    if ":" in ident:
        return ident.split(":", 1)[1].strip()
    return ident.strip()


def details_name(details: str) -> str:
    return details.split(",", 1)[0].strip()


def parse_level(details: str) -> str:
    match = re.search(r"\bL(\d+)\b", details)
    return match.group(1) if match else "?"


def details_is_shiny(details: str) -> bool:
    return ", shiny" in details.lower()


def normalize_effect_name(value: str) -> str:
    text = str(value or "").strip()
    if ":" in text:
        text = text.split(":", 1)[1]
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def protocol_tag_values(args: list[str], tag: str) -> list[str]:
    prefix = f"[{tag}]"
    values: list[str] = []
    for arg in args:
        text = str(arg or "").strip()
        if text.startswith(prefix):
            values.append(text[len(prefix):].strip())
    return values


def effect_label(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "effect"
    lowered = text.lower()
    if lowered in STATUS_MAP:
        return STATUS_MAP[lowered]
    if ":" in text:
        text = text.split(":", 1)[1].strip()
    return text.replace("-", " ").strip() or "effect"


def pokemon_key(slot: str, name_or_details: str) -> str:
    return f"{slot}:{details_name(name_or_details).strip().lower()}"


def parse_condition(condition: str) -> dict[str, Any]:
    raw = condition.strip()
    parsed = {
        "raw": raw,
        "hp_text": raw or "unknown",
        "current_hp": None,
        "max_hp": None,
        "percent": None,
        "status": "",
        "fainted": False,
    }
    if not raw:
        return parsed
    if "fnt" in raw.lower():
        parsed["hp_text"] = "0/0"
        parsed["percent"] = 0
        parsed["status"] = "FNT"
        parsed["fainted"] = True
        return parsed

    parts = raw.split()
    hp_text = parts[0]
    if "/" in hp_text:
        current, maximum = hp_text.split("/", 1)
        if current.isdigit() and maximum.isdigit():
            parsed["current_hp"] = int(current)
            parsed["max_hp"] = int(maximum)
            parsed["percent"] = round((int(current) / int(maximum)) * 100) if int(maximum) else 0
    elif hp_text.endswith("%"):
        digits = hp_text[:-1]
        if digits.isdigit():
            parsed["percent"] = int(digits)
    elif hp_text.isdigit():
        parsed["current_hp"] = int(hp_text)

    parsed["hp_text"] = (
        f"{parsed['current_hp']}/{parsed['max_hp']}"
        if parsed["current_hp"] is not None and parsed["max_hp"] is not None
        else hp_text
    )
    if len(parts) > 1:
        parsed["status"] = STATUS_MAP.get(parts[1], parts[1].upper())
    return parsed


def format_condition(condition: str) -> str:
    parsed = parse_condition(condition)
    if parsed["fainted"]:
        return "fainted"
    return f"{parsed['hp_text']} {parsed['status']}".strip() or "unknown"


def fainted(condition: str) -> bool:
    return "fnt" in condition.lower()


def clean_error(message: str) -> str:
    message = re.sub(r"^\[[^\]]+\]\s*", "", message).strip()
    return message or "That choice was rejected by the simulator."


@dataclass
class PublicBattleView:
    player_names: dict[str, str]
    turn: int = 0
    active: dict[str, dict[str, Any]] = field(default_factory=dict)
    known_pokemon: dict[str, dict[str, Any]] = field(default_factory=dict)
    current_items: dict[str, str | None] = field(default_factory=dict)
    weather: str = ""
    terrain: str = ""
    room_effects: set[str] = field(default_factory=set)
    recent: list[str] = field(default_factory=list)
    last_turn_recent: list[str] = field(default_factory=list)
    history: list[str] = field(default_factory=list)
    winner: str | None = None
    tie: bool = False
    last_move_event: dict[str, Any] | None = None

    def apply_request(self, slot: str, request: dict[str, Any]) -> None:
        side = request.get("side") or {}
        pokemon_list = side.get("pokemon") or []
        for pokemon in pokemon_list:
            details = str(pokemon.get("details", pokemon.get("ident", "")))
            if not details:
                continue
            key = pokemon_key(slot, details)
            entry = self.known_pokemon.setdefault(
                key,
                {
                    "name": details_name(details),
                    "details": details,
                    "level": parse_level(details),
                    "types": [],
                },
            )
            entry["name"] = details_name(details)
            entry["details"] = details
            entry["level"] = parse_level(details)
            entry["shiny"] = bool(pokemon.get("shiny")) or details_is_shiny(details)
            display_types = list(pokemon.get("displayTypes") or [])
            if display_types:
                entry["types"] = display_types

            if pokemon.get("active"):
                self._set_active(
                    slot,
                    details,
                    str(pokemon.get("condition", "")),
                    types=display_types,
                    preserve_item=True,
                )
                active_request = ((request.get("active") or [{}]) or [{}])[0]
                if slot in self.active:
                    is_dynamaxed = bool(active_request.get("maxMoves")) and not bool(active_request.get("canDynamax"))
                    self.active[slot]["dynamaxed"] = is_dynamaxed
                    self.active[slot]["gigantamax_species"] = (
                        str((active_request.get("maxMoves") or {}).get("gigantamax") or "").strip()
                        if is_dynamaxed
                        else ""
                    )

    def apply_lines(self, lines: list[str]) -> None:
        for line in lines:
            command, args = protocol_parts(line)
            if not command:
                continue

            if command == "player" and len(args) >= 2:
                self.player_names[args[0]] = args[1]
            elif command == "turn" and args:
                if self.recent:
                    self.last_turn_recent = list(self.recent)
                    self.recent.clear()
                self.last_move_event = None
                try:
                    self.turn = int(args[0])
                except ValueError:
                    pass
            elif command in {"switch", "drag", "replace"} and len(args) >= 3:
                side = ident_side(args[0])
                details = args[1]
                name = details_name(details)
                self._trim_recent_for_switch()
                self._set_active(side, details, args[2], preserve_item=False)
                action = {
                    "switch": "sent out",
                    "drag": "dragged in",
                    "replace": "revealed",
                }[command]
                self._add_recent(
                    f"{self.player_names.get(side, side)} {action} {name} ({format_condition(args[2])})."
                )
            elif command == "move" and len(args) >= 2:
                attacker_ident = args[0]
                attacker = ident_name(args[0])
                move_name = args[1]
                target = ident_name(args[2]) if len(args) >= 3 and args[2] else ""
                self.last_move_event = {
                    "attacker": attacker,
                    "attacker_ident": attacker_ident,
                    "attacker_key": normalize_effect_name(attacker),
                    "move_name": move_name,
                    "target": target,
                    "target_key": normalize_effect_name(target),
                    "turn": self.turn,
                }
                if target and normalize_effect_name(target) != normalize_effect_name(attacker):
                    self._add_recent(f"{attacker} used {move_name} on {target}.")
                else:
                    self._add_recent(f"{attacker} used {move_name}.")
                self.last_move_event["recent_index"] = len(self.recent) - 1
            elif command == "-miss" and len(args) >= 2:
                self.last_move_event = None
                self._add_recent(f"{ident_name(args[0])}'s move missed {ident_name(args[1])}.")
            elif command in {"-damage", "-heal"} and len(args) >= 2:
                side = ident_side(args[0])
                name = ident_name(args[0])
                previous_hp = None
                previous_max_hp = None
                previous_percent = None
                if side and side in self.active:
                    previous_hp = self.active[side].get("current_hp")
                    previous_max_hp = self.active[side].get("max_hp")
                    previous_percent = self.active[side].get("percent")

                parsed = parse_condition(args[1])
                can_compute_exact = (
                    previous_hp is not None
                    and previous_max_hp is not None
                    and parsed["current_hp"] is not None
                    and parsed["max_hp"] is not None
                    and int(parsed["max_hp"]) == int(previous_max_hp)
                )

                if side and side in self.active:
                    if can_compute_exact:
                        self._apply_condition(self.active[side], args[1])
                    else:
                        self.active[side]["percent"] = parsed["percent"]
                        self.active[side]["status"] = parsed["status"]
                        self.active[side]["condition"] = parsed["raw"]

                if command == "-damage":
                    delta_hp = None
                    if can_compute_exact:
                        delta_hp = max(int(previous_hp) - int(parsed["current_hp"]), 0)
                    elif parsed["fainted"] and previous_hp is not None:
                        delta_hp = max(int(previous_hp), 0)
                    delta_percent = None
                    if previous_percent is not None and parsed["percent"] is not None:
                        delta_percent = max(int(previous_percent) - int(parsed["percent"]), 0)

                    move_context = self._direct_damage_context(name, args[2:])
                    if move_context is not None:
                        amount_text = self._format_delta_amount(delta_hp, delta_percent)
                        if amount_text:
                            combined = self._move_damage_summary(move_context, target_name=name, amount_text=amount_text)
                            if not self._replace_recent_at(int(move_context.get("recent_index", -1)), combined):
                                self._add_recent(combined)
                        else:
                            # Keep the original move line when no concrete damage amount can be shown.
                            pass
                    else:
                        source = self._damage_source_label(args[2:])
                        amount_text = self._format_delta_amount(delta_hp, delta_percent)
                        if source:
                            if amount_text:
                                self._add_recent(f"{name} lost {amount_text} from {source}.")
                            else:
                                self._add_recent(f"{name} was hurt by {source}.")
                        elif amount_text:
                            self._add_recent(f"{name} lost {amount_text}.")
                        else:
                            self._add_recent(f"{name} was damaged.")
                else:
                    delta_hp = None
                    if can_compute_exact:
                        delta_hp = max(int(parsed["current_hp"]) - int(previous_hp), 0)
                    delta_percent = None
                    if previous_percent is not None and parsed["percent"] is not None:
                        delta_percent = max(int(parsed["percent"]) - int(previous_percent), 0)
                    amount_text = self._format_delta_amount(delta_hp, delta_percent)
                    if amount_text:
                        self._add_recent(f"{name} recovered {amount_text}.")
                    else:
                        self._add_recent(f"{name} recovered HP.")
            elif command == "-crit" and args:
                self._add_recent(f"Critical hit on {ident_name(args[0])}.")
            elif command == "-supereffective" and args:
                self._add_recent(f"It was super effective against {ident_name(args[0])}.")
            elif command == "-resisted" and args:
                self._add_recent(f"It was not very effective against {ident_name(args[0])}.")
            elif command == "-immune" and args:
                self._add_recent(f"{ident_name(args[0])} was immune.")
            elif command == "faint" and args:
                side = ident_side(args[0])
                name = ident_name(args[0])
                if side and side in self.active:
                    self._apply_condition(self.active[side], "0/0 fnt")
                self._add_recent(f"{name} fainted.")
            elif command == "-status" and len(args) >= 2:
                side = ident_side(args[0])
                if side and side in self.active:
                    self.active[side]["status"] = STATUS_MAP.get(args[1], args[1].upper())
                self._add_recent(f"{ident_name(args[0])} is now {STATUS_MAP.get(args[1], args[1].upper())}.")
            elif command == "-curestatus" and len(args) >= 2:
                side = ident_side(args[0])
                if side and side in self.active:
                    self.active[side]["status"] = ""
                self._add_recent(f"{ident_name(args[0])} is no longer {STATUS_MAP.get(args[1], args[1].upper())}.")
            elif command in {"-boost", "-unboost"} and len(args) >= 3:
                stat_name = STAT_NAMES.get(args[1], args[1])
                amount = int(args[2])
                verb = "rose" if command == "-boost" else "fell"
                self._add_recent(f"{ident_name(args[0])}'s {stat_name} {verb} by {abs(amount)}.")
            elif command == "-terastallize" and len(args) >= 2:
                side = ident_side(args[0])
                if side and side in self.active:
                    self.active[side]["types"] = [args[1]]
                self._add_recent(f"{ident_name(args[0])} terastallized into {args[1]}.")
            elif command == "-mega" and args:
                continue
            elif command == "-zpower" and args:
                continue
            elif command == "-start" and len(args) >= 2 and args[1] == "Dynamax":
                side = ident_side(args[0])
                if side and side in self.active:
                    self.active[side]["dynamaxed"] = True
                self._add_recent(f"{ident_name(args[0])} dynamaxed.")
            elif command == "-end" and len(args) >= 2 and args[1] == "Dynamax":
                side = ident_side(args[0])
                if side and side in self.active:
                    self.active[side]["dynamaxed"] = False
                    self.active[side]["gigantamax_species"] = ""
                self._add_recent(f"{ident_name(args[0])} returned to normal size.")
            elif command in {"detailschange", "-formechange"} and len(args) >= 2:
                side = ident_side(args[0])
                name = details_name(args[1])
                if side and side in self.active:
                    self._update_active_details(side, args[1])
                self._add_recent(f"{ident_name(args[0])} became {name}.")
            elif command in {"-item", "item"} and len(args) >= 2:
                side = ident_side(args[0])
                if side and side in self.active:
                    key = self.active[side]["key"]
                    self.current_items[key] = args[1]
                    self.active[side]["item"] = args[1]
                self._add_recent(f"{ident_name(args[0])} revealed {args[1]}.")
            elif command == "-enditem" and len(args) >= 2:
                side = ident_side(args[0])
                if side and side in self.active:
                    key = self.active[side]["key"]
                    self.current_items[key] = None
                    self.active[side]["item"] = None
                self._add_recent(f"{ident_name(args[0])} lost {args[1]}.")
            elif command == "-weather" and args:
                weather_key = normalize_effect_name(args[0])
                self.weather = "" if weather_key == "none" else weather_key
                if args[0] == "none":
                    self._add_recent("The weather cleared.")
                else:
                    self._add_recent(f"Weather: {args[0]}.")
            elif command == "-fieldstart" and args:
                effect_key = normalize_effect_name(args[0])
                if effect_key.endswith("terrain"):
                    self.terrain = effect_key
                elif effect_key.endswith("room"):
                    self.room_effects.add(effect_key)
            elif command == "-fieldend" and args:
                effect_key = normalize_effect_name(args[0])
                if effect_key.endswith("terrain") and self.terrain == effect_key:
                    self.terrain = ""
                elif effect_key.endswith("room"):
                    self.room_effects.discard(effect_key)
            elif command == "-ability" and len(args) >= 2:
                self._add_recent(f"{ident_name(args[0])} revealed {args[1]}.")
            elif command == "cant" and len(args) >= 2:
                reason = args[2] if len(args) >= 3 and args[2] else args[1]
                self._add_recent(f"{ident_name(args[0])} could not move ({reason}).")
            elif command == "win" and args:
                self.winner = args[0]
                self.tie = False
                self._add_recent(f"{args[0]} wins the battle.")
            elif command == "tie":
                self.winner = None
                self.tie = True
                self._add_recent("The battle ended in a tie.")
            elif command == "-message" and args:
                self._add_recent(args[0])

    def display_recent(self) -> list[str]:
        return self.recent or self.last_turn_recent

    def _set_active(
        self,
        slot: str,
        details: str,
        condition: str,
        *,
        types: list[str] | None = None,
        preserve_item: bool,
    ) -> None:
        if not slot:
            return
        key = pokemon_key(slot, details)
        entry = self.known_pokemon.setdefault(
            key,
            {
                "name": details_name(details),
                "details": details,
                "level": parse_level(details),
                "types": [],
                "shiny": details_is_shiny(details),
            },
        )
        entry["name"] = details_name(details)
        entry["details"] = details
        entry["level"] = parse_level(details)
        entry["shiny"] = bool(entry.get("shiny")) or details_is_shiny(details)
        if types:
            entry["types"] = list(types)

        previous = self.active.get(slot)
        item = self.current_items.get(key)
        if preserve_item and item is None and previous and previous.get("key") == key:
            item = previous.get("item")

        state = {
            "key": key,
            "name": entry["name"],
            "details": details,
            "level": entry["level"],
            "types": list(entry.get("types") or []),
            "item": item,
            "shiny": bool(entry.get("shiny")),
            "dynamaxed": bool(previous.get("dynamaxed")) if previous else False,
            "gigantamax_species": str(previous.get("gigantamax_species") or "") if previous else "",
        }
        self._apply_condition(state, condition)
        self.active[slot] = state

    def _apply_condition(self, state: dict[str, Any], condition: str) -> None:
        parsed = parse_condition(condition)
        state["condition"] = parsed["raw"]
        state["hp_text"] = parsed["hp_text"]
        state["current_hp"] = parsed["current_hp"]
        state["max_hp"] = parsed["max_hp"]
        state["percent"] = parsed["percent"]
        state["status"] = parsed["status"]
        state["fainted"] = parsed["fainted"]

    def _update_active_details(self, slot: str, details: str) -> None:
        active = self.active.get(slot)
        if not active:
            return
        condition = str(active.get("condition", ""))
        item = active.get("item")
        types = active.get("types") or []
        self._set_active(slot, details, condition, types=list(types), preserve_item=False)
        if item is not None and slot in self.active:
            self.active[slot]["item"] = item
            self.current_items[self.active[slot]["key"]] = item

    def _add_history(self, message: str) -> None:
        message = message.strip()
        if not message:
            return
        if self.history and self.history[-1] == message:
            return
        self.history.append(message)

    def _add_recent(self, message: str) -> None:
        message = message.strip()
        if not message:
            return
        if self.recent and self.recent[-1] == message:
            return
        self.recent.append(message)
        self._add_history(message)

    def _trim_recent_for_switch(self) -> None:
        if not self.recent:
            return
        switch_markers = (" sent out ", " dragged in ", " revealed ")
        if all(any(marker in entry for marker in switch_markers) for entry in self.recent):
            return
        self.recent.clear()

    def _replace_recent_at(self, index: int, message: str) -> bool:
        if index < 0 or index >= len(self.recent):
            return False
        previous = self.recent[index]
        self.recent[index] = message
        if self.history and self.history[-1] == previous:
            self.history[-1] = message
        return True

    def _move_damage_summary(self, context: dict[str, Any], *, target_name: str, amount_text: str) -> str:
        attacker = str(context.get("attacker") or "Pokemon")
        move_name = str(context.get("move_name") or "its move")
        target = str(target_name or context.get("target") or "").strip()
        attacker_key = normalize_effect_name(attacker)
        target_key = normalize_effect_name(target)
        if target and target_key and target_key != attacker_key:
            return f"{attacker} used {move_name} on {target} Dealt {amount_text}."
        return f"{attacker} used {move_name} Dealt {amount_text}."

    def _direct_damage_context(self, target_name: str, extra_args: list[str]) -> dict[str, Any] | None:
        context = self.last_move_event
        if context is None:
            return None
        if int(context.get("turn") or 0) != int(self.turn or 0):
            return None
        target_key = normalize_effect_name(target_name)
        if not target_key or target_key != str(context.get("target_key") or ""):
            return None
        from_values = protocol_tag_values(extra_args, "from")
        if from_values:
            move_key = normalize_effect_name(str(context.get("move_name") or ""))
            if any(normalize_effect_name(value) != move_key for value in from_values):
                return None
        return context

    def _damage_source_label(self, extra_args: list[str]) -> str | None:
        from_values = protocol_tag_values(extra_args, "from")
        if not from_values:
            return None
        return effect_label(from_values[0])

    def _format_delta_amount(self, delta_hp: int | None, delta_percent: int | None) -> str | None:
        if delta_hp is not None and delta_hp > 0:
            return f"{delta_hp} HP"
        if delta_percent is not None and delta_percent > 0:
            return f"{delta_percent}%"
        return None
