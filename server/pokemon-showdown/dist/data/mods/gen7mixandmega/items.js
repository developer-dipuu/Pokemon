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
var items_exports = {};
__export(items_exports, {
  Items: () => Items
});
module.exports = __toCommonJS(items_exports);
const Items = {
  blueorb: {
    inherit: true,
    onSwitchIn(pokemon) {
      if (pokemon.isActive && !pokemon.species.isPrimal && !pokemon.transformed) {
        const species = this.actions.getMixedSpecies(pokemon.m.originalSpecies, "Kyogre-Primal", pokemon);
        if (pokemon.m.originalSpecies === "Kyogre") {
          pokemon.formeChange(species, this.effect, true);
        } else {
          pokemon.formeChange(species, this.effect, true);
          pokemon.baseSpecies = species;
          this.add("-start", pokemon, "Blue Orb", "[silent]");
        }
      }
    },
    onTakeItem: false
  },
  redorb: {
    inherit: true,
    onSwitchIn(pokemon) {
      if (pokemon.isActive && !pokemon.species.isPrimal && !pokemon.transformed) {
        const species = this.actions.getMixedSpecies(pokemon.m.originalSpecies, "Groudon-Primal", pokemon);
        if (pokemon.m.originalSpecies === "Groudon") {
          pokemon.formeChange(species, this.effect, true);
        } else {
          pokemon.formeChange(species, this.effect, true);
          pokemon.baseSpecies = species;
          this.add("-start", pokemon, "Red Orb", "[silent]");
          const apparentSpecies = pokemon.illusion ? pokemon.illusion.species.name : pokemon.m.originalSpecies;
          const oSpecies = this.dex.species.get(apparentSpecies);
          if (pokemon.illusion) {
            const types = oSpecies.types;
            if (types.length > 1 || types[types.length - 1] !== "Fire") {
              this.add("-start", pokemon, "typechange", (types[0] !== "Fire" ? types[0] + "/" : "") + "Fire", "[silent]");
            }
          } else if (oSpecies.types.length !== pokemon.species.types.length || oSpecies.types[1] !== pokemon.species.types[1]) {
            this.add("-start", pokemon, "typechange", pokemon.species.types.join("/"), "[silent]");
          }
        }
      }
    },
    onTakeItem: false
  }
};
//# sourceMappingURL=items.js.map
