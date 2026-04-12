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
var scripts_exports = {};
__export(scripts_exports, {
  Scripts: () => Scripts
});
module.exports = __toCommonJS(scripts_exports);
const Scripts = {
  gen: 9,
  inherit: "gen9",
  runAction(action) {
    if (action.choice === "runDynamax") {
      action.pokemon.addVolatile("dynamax");
      action.pokemon.side.fullGimmickDynamaxUsed = true;
      action.pokemon.side.dynamaxUsed = true;
      if (action.pokemon.side.allySide) {
        action.pokemon.side.allySide.fullGimmickDynamaxUsed = true;
        action.pokemon.side.allySide.dynamaxUsed = true;
      }
      return;
    }
    return Object.getPrototypeOf(this).runAction.call(this, action);
  },
  actions: {
    inherit: true,
    canTerastallize(pokemon) {
      if (this.dex.gen !== 9) {
        return null;
      }
      return pokemon.teraType;
    }
  },
  pokemon: {
    inherit: true,
    getDynamaxRequest(skipChecks) {
      if (!skipChecks) {
        if (!this.side.canDynamaxNow()) return;
        if (this.species.isMega || this.species.isPrimal || this.species.forme === "Ultra") {
          return;
        }
        if (this.species.cannotDynamax || this.illusion?.species.cannotDynamax) return;
      }
      const result = { maxMoves: [] };
      let atLeastOne = false;
      for (const moveSlot of this.moveSlots) {
        const move = this.battle.dex.moves.get(moveSlot.id);
        const maxMove = this.battle.actions.getMaxMove(move, this);
        if (maxMove) {
          if (this.maxMoveDisabled(move)) {
            result.maxMoves.push({ move: maxMove.id, target: maxMove.target, disabled: true });
          } else {
            result.maxMoves.push({ move: maxMove.id, target: maxMove.target });
            atLeastOne = true;
          }
        }
      }
      if (!atLeastOne) return;
      if (this.canGigantamax && this.gigantamax) result.gigantamax = this.canGigantamax;
      return result;
    }
  },
  side: {
    inherit: true,
    canDynamaxNow() {
      return !this.fullGimmickDynamaxUsed;
    }
  }
};
//# sourceMappingURL=scripts.js.map
