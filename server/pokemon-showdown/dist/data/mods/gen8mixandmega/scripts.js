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
  gen: 8,
  inherit: "gen8",
  init() {
    for (const i in this.data.Items) {
      const item = this.data.Items[i];
      if (!item.megaStone || item.isNonstandard !== "Past") continue;
      this.modData("Items", i).onTakeItem = false;
      this.modData("Items", i).isNonstandard = null;
      if (item.megaStone) {
        for (const megaEvo of Object.values(item.megaStone)) {
          this.modData("FormatsData", this.toID(megaEvo)).isNonstandard = null;
        }
      }
    }
  },
  actions: {
    canMegaEvo(pokemon) {
      if (pokemon.species.isMega) return null;
      const item = pokemon.getItem();
      if (!item.megaStone) return null;
      return Object.values(item.megaStone)[0];
    },
    runMegaEvo(pokemon) {
      if (pokemon.species.isMega) return false;
      const species = this.getMixedSpecies(pokemon.m.originalSpecies, pokemon.canMegaEvo, pokemon);
      const oSpecies = this.dex.species.get(pokemon.m.originalSpecies);
      const oMegaSpecies = this.dex.species.get(species.originalSpecies);
      pokemon.formeChange(species, pokemon.getItem(), true);
      this.battle.add("-start", pokemon, oMegaSpecies.requiredItem, "[silent]");
      if (oSpecies.types.join("/") !== pokemon.species.types.join("/")) {
        this.battle.add("-start", pokemon, "typechange", pokemon.species.types.join("/"), "[silent]");
      }
      pokemon.canMegaEvo = false;
      return true;
    },
    getMixedSpecies(originalForme, formeChange, pokemon) {
      const originalSpecies = this.dex.species.get(originalForme);
      const formeChangeSpecies = this.dex.species.get(formeChange);
      const deltas = this.getFormeChangeDeltas(formeChangeSpecies, pokemon);
      const species = this.mutateOriginalSpecies(originalSpecies, deltas);
      return species;
    },
    getFormeChangeDeltas(formeChangeSpecies, pokemon) {
      let baseSpecies = this.dex.species.get(formeChangeSpecies.isMega ? formeChangeSpecies.battleOnly : formeChangeSpecies.baseSpecies);
      if (formeChangeSpecies.name === "Zygarde-Mega") {
        baseSpecies = this.dex.species.get("Zygarde-Complete");
      }
      const deltas = {
        ability: formeChangeSpecies.abilities["0"],
        baseStats: {},
        weighthg: formeChangeSpecies.weighthg - baseSpecies.weighthg,
        heightm: (formeChangeSpecies.heightm * 10 - baseSpecies.heightm * 10) / 10,
        originalSpecies: formeChangeSpecies.name,
        requiredItem: formeChangeSpecies.requiredItem
      };
      let statId;
      for (statId in formeChangeSpecies.baseStats) {
        deltas.baseStats[statId] = formeChangeSpecies.baseStats[statId] - baseSpecies.baseStats[statId];
      }
      if (formeChangeSpecies.types.length > baseSpecies.types.length) {
        deltas.type = formeChangeSpecies.types[1];
      } else if (formeChangeSpecies.types.length < baseSpecies.types.length) {
        deltas.type = "mono";
      } else if (formeChangeSpecies.types[1] !== baseSpecies.types[1]) {
        deltas.type = formeChangeSpecies.types[1];
      }
      deltas.isMega = true;
      return deltas;
    },
    mutateOriginalSpecies(speciesOrForme, deltas) {
      if (!deltas) throw new TypeError("Must specify deltas!");
      const species = this.dex.deepClone(this.dex.species.get(speciesOrForme));
      species.abilities = { "0": deltas.ability };
      if (species.types[0] === deltas.type) {
        species.types = [deltas.type];
      } else if (deltas.type === "mono") {
        species.types = [species.types[0]];
      } else if (deltas.type) {
        species.types = [species.types[0], deltas.type];
      }
      const baseStats = species.baseStats;
      for (const statName in baseStats) {
        baseStats[statName] = this.battle.clampIntRange(baseStats[statName] + deltas.baseStats[statName], 1, 255);
      }
      species.weighthg = Math.max(1, species.weighthg + deltas.weighthg);
      species.heightm = Math.max(0.1, (species.heightm * 10 + deltas.heightm * 10) / 10);
      species.originalSpecies = deltas.originalSpecies;
      species.requiredItem = deltas.requiredItem;
      if (deltas.isMega) species.isMega = true;
      return species;
    }
  }
};
//# sourceMappingURL=scripts.js.map
