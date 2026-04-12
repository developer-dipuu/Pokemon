"use strict";
var __defProp = Object.defineProperty;
var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
var __getOwnPropNames = Object.getOwnPropertyNames;
var __hasOwnProp = Object.prototype.hasOwnProperty;
var __export = (target, all) => {
  for (var name in all)
    __defProp(target, name, { get: all[name], enumerable: true });
};
var __copyProps = (to, from, except, desc) => {
  if (from && typeof from === "object" || typeof from === "function") {
    for (let key of __getOwnPropNames(from))
      if (!__hasOwnProp.call(to, key) && key !== except)
        __defProp(to, key, { get: () => from[key], enumerable: !(desc = __getOwnPropDesc(from, key)) || desc.enumerable });
  }
  return to;
};
var __toCommonJS = (mod) => __copyProps(__defProp({}, "__esModule", { value: true }), mod);
var moves_exports = {};
__export(moves_exports, {
  Moves: () => Moves
});
module.exports = __toCommonJS(moves_exports);
const Moves = {
  stealthrock: {
    inherit: true,
    condition: {
      // this is a side condition
      onSideStart(side) {
        this.add("-sidestart", side, "move: Stealth Rock");
      },
      onSwitchIn(pokemon) {
        if (pokemon.hasItem("heavydutyboots") || pokemon.hasAbility("exoskeleton")) return;
        const typeMod = this.clampIntRange(pokemon.runEffectiveness(this.dex.getActiveMove("stealthrock")), -6, 6);
        this.damage(pokemon.maxhp * 2 ** typeMod / 8);
      }
    }
  },
  toxicspikes: {
    inherit: true,
    condition: {
      // this is a side condition
      onSideStart(side) {
        this.add("-sidestart", side, "move: Toxic Spikes");
        this.effectState.layers = 1;
      },
      onSideRestart(side) {
        if (this.effectState.layers >= 2) return false;
        this.add("-sidestart", side, "move: Toxic Spikes");
        this.effectState.layers++;
      },
      onSwitchIn(pokemon) {
        if (!pokemon.isGrounded()) return;
        if (pokemon.hasType("Poison")) {
          this.add("-sideend", pokemon.side, "move: Toxic Spikes", `[of] ${pokemon}`);
          pokemon.side.removeSideCondition("toxicspikes");
        } else if (pokemon.hasType("Steel") || pokemon.hasItem("heavydutyboots") || pokemon.hasAbility("exoskeleton")) {
        } else if (this.effectState.layers >= 2) {
          pokemon.trySetStatus("tox", pokemon.side.foe.active[0]);
        } else {
          pokemon.trySetStatus("psn", pokemon.side.foe.active[0]);
        }
      }
    }
  },
  spikes: {
    inherit: true,
    condition: {
      // this is a side condition
      onSideStart(side) {
        this.add("-sidestart", side, "Spikes");
        this.effectState.layers = 1;
      },
      onSideRestart(side) {
        if (this.effectState.layers >= 3) return false;
        this.add("-sidestart", side, "Spikes");
        this.effectState.layers++;
      },
      onSwitchIn(pokemon) {
        if (!pokemon.isGrounded() || pokemon.hasItem("heavydutyboots") || pokemon.hasAbility("exoskeleton")) return;
        const damageAmounts = [0, 3, 4, 6];
        this.damage(damageAmounts[this.effectState.layers] * pokemon.maxhp / 24);
      }
    }
  },
  stickyweb: {
    inherit: true,
    condition: {
      onSideStart(side) {
        this.add("-sidestart", side, "move: Sticky Web");
      },
      onSwitchIn(pokemon) {
        if (!pokemon.isGrounded() || pokemon.hasItem("heavydutyboots") || pokemon.hasAbility("exoskeleton")) return;
        this.add("-activate", pokemon, "move: Sticky Web");
        this.boost({ spe: -1 }, pokemon, pokemon.side.foe.active[0], this.dex.getActiveMove("stickyweb"));
      }
    }
  }
};
//# sourceMappingURL=moves.js.map
