"use strict";

const fs = require("node:fs");
const path = require("node:path");
const readline = require("node:readline");

const showdownDir = path.resolve(__dirname, "..", "..", "server", "pokemon-showdown");
const distEntry = path.join(showdownDir, "dist", "sim", "index.js");

function emit(payload) {
	process.stdout.write(`${JSON.stringify(payload)}\n`);
}

class DexToolError extends Error {
	constructor(message) {
		super(message);
		this.name = "DexToolError";
	}
}

function emitError(message) {
	emit({ok: false, error: message});
}

function fail(message) {
	throw new DexToolError(message);
}

if (!fs.existsSync(distEntry)) {
	emitError(`Pokemon Showdown is not built yet. Expected ${distEntry}.`);
	process.exit(0);
}

const {Dex, TeamValidator, Teams} = require(distEntry);

const NATURES = {
	Hardy: [null, null],
	Lonely: ["atk", "def"],
	Brave: ["atk", "spe"],
	Adamant: ["atk", "spa"],
	Naughty: ["atk", "spd"],
	Bold: ["def", "atk"],
	Docile: [null, null],
	Relaxed: ["def", "spe"],
	Impish: ["def", "spa"],
	Lax: ["def", "spd"],
	Timid: ["spe", "atk"],
	Hasty: ["spe", "def"],
	Serious: [null, null],
	Jolly: ["spe", "spa"],
	Naive: ["spe", "spd"],
	Modest: ["spa", "atk"],
	Mild: ["spa", "def"],
	Quiet: ["spa", "spe"],
	Bashful: [null, null],
	Rash: ["spa", "spd"],
	Calm: ["spd", "atk"],
	Gentle: ["spd", "def"],
	Sassy: ["spd", "spe"],
	Careful: ["spd", "spa"],
	Quirky: [null, null],
};

function sample(items) {
	return items[Math.floor(Math.random() * items.length)];
}

function shuffle(items) {
	const cloned = [...items];
	for (let index = cloned.length - 1; index > 0; index -= 1) {
		const swapIndex = Math.floor(Math.random() * (index + 1));
		[cloned[index], cloned[swapIndex]] = [cloned[swapIndex], cloned[index]];
	}
	return cloned;
}

function parsePayload(raw) {
	try {
		return JSON.parse(String(raw || "{}"));
	} catch (error) {
		fail(`Invalid JSON payload: ${error.message}`);
	}
}

function readJson(filePath) {
	return JSON.parse(fs.readFileSync(filePath, "utf8"));
}

function getDex(mod) {
	if (!mod || mod === "gen9") return Dex;
	return Dex.mod(mod);
}

function resolveSpecies(dex, rawSpecies) {
	const text = String(rawSpecies || "").trim();
	const candidates = [];
	const pushCandidate = value => {
		const candidate = String(value || "").trim();
		if (!candidate || candidates.includes(candidate)) return;
		candidates.push(candidate);
	};

	pushCandidate(text);
	pushCandidate(text.replace(/♀/g, "-F").replace(/♂/g, ""));
	pushCandidate(text.replace(/♀/g, "-female").replace(/♂/g, "-male"));

	const femaleBase = text.replace(/(?:[-\s_]*(female|f))$/i, "").trim();
	if (femaleBase && femaleBase !== text) {
		pushCandidate(`${femaleBase}-F`);
		pushCandidate(femaleBase);
	}

	const maleBase = text.replace(/(?:[-\s_]*(male|m))$/i, "").trim();
	if (maleBase && maleBase !== text) {
		pushCandidate(maleBase);
		pushCandidate(`${maleBase}-M`);
	}

	for (const candidate of candidates) {
		const species = dex.species.get(candidate);
		if (species.exists) return species;
	}
	return dex.species.get(text);
}

function buildLevelMoveRecord(sources, maxGen) {
	let best = null;
	for (const source of sources || []) {
		const match = String(source).match(/^(\d)L(\d+)$/);
		if (!match) continue;
		const gen = Number(match[1]);
		const level = Number(match[2]);
		if (gen > maxGen) continue;
		if (!best || gen > best.gen || (gen === best.gen && level < best.level)) {
			best = {gen, level};
		}
	}
	return best;
}

function levelUpMoveEntries(dex, species, level) {
	const byMove = new Map();
	for (const learnsetBlock of dex.species.getFullLearnset(species.id)) {
		const learnset = learnsetBlock.learnset || {};
		for (const moveid of Object.keys(learnset)) {
			const record = buildLevelMoveRecord(learnset[moveid], dex.gen);
			if (!record || record.level > level) continue;
			const previous = byMove.get(moveid);
			if (!previous || record.gen > previous.gen || (record.gen === previous.gen && record.level < previous.level)) {
				byMove.set(moveid, record);
			}
		}
	}

	return [...byMove.entries()]
		.map(([moveid, record]) => {
			const move = dex.moves.get(moveid);
			return {
				id: moveid,
				name: move.name,
				level: record.level,
			};
		})
		.filter(move => move.name && move.name !== "Struggle" && !dex.moves.get(move.id).isNonstandard)
		.sort((left, right) => left.level - right.level || left.name.localeCompare(right.name));
}

function trainingMoveEntries(dex, species) {
	const byMove = new Map();
	for (const learnsetBlock of dex.species.getFullLearnset(species.id)) {
		const learnset = learnsetBlock.learnset || {};
		for (const moveid of Object.keys(learnset)) {
			const sources = Array.isArray(learnset[moveid]) ? learnset[moveid] : [];
			let levelRecord = null;
			let machine = false;
			let tutor = false;

			for (const source of sources) {
				const levelMatch = String(source).match(/^(\d)L(\d+)$/);
				if (levelMatch) {
					const gen = Number(levelMatch[1]);
					const level = Number(levelMatch[2]);
					if (gen <= dex.gen && (!levelRecord || gen > levelRecord.gen || (gen === levelRecord.gen && level < levelRecord.level))) {
						levelRecord = {gen, level};
					}
					continue;
				}
				if (/^\d+M$/.test(String(source))) {
					machine = true;
					continue;
				}
				if (/^\d+T$/.test(String(source))) {
					tutor = true;
				}
			}

			if (!levelRecord && !machine && !tutor) continue;
			const existing = byMove.get(moveid) || {
				id: moveid,
				methods: new Set(),
				level: null,
			};
			if (levelRecord) {
				existing.methods.add("Level Up");
				if (existing.level === null || levelRecord.level < existing.level) {
					existing.level = levelRecord.level;
				}
			}
			if (machine) existing.methods.add("TM");
			if (tutor) existing.methods.add("Tutor");
			byMove.set(moveid, existing);
		}
	}

	return [...byMove.values()]
		.map(entry => {
			const move = dex.moves.get(entry.id);
			return {
				id: entry.id,
				name: move.name,
				level: entry.level,
				methods: [...entry.methods],
			};
		})
		.filter(move => move.name && move.name !== "Struggle" && !dex.moves.get(move.id).isNonstandard)
		.sort((left, right) => left.name.localeCompare(right.name));
}

function eggMoveEntries(dex, species) {
	const moves = new Map();
	for (const learnsetBlock of dex.species.getFullLearnset(species.id)) {
		const learnset = learnsetBlock.learnset || {};
		for (const moveid of Object.keys(learnset)) {
			const sources = Array.isArray(learnset[moveid]) ? learnset[moveid] : [];
			if (!sources.some(source => /^\d+E$/.test(String(source)))) continue;
			const move = dex.moves.get(moveid);
			if (!move.exists || move.isNonstandard || move.name === "Struggle") continue;
			moves.set(moveid, move.name);
		}
	}
	return [...moves.entries()]
		.map(([id, name]) => ({id, name}))
		.sort((left, right) => left.name.localeCompare(right.name));
}

function baseEggSpecies(dex, species) {
	let current = species;
	while (current?.prevo) {
		const nextSpecies = dex.species.get(current.prevo);
		if (!nextSpecies.exists) break;
		current = nextSpecies;
	}
	return current?.exists ? current : species;
}

function breedingProfilePayload(dex, species) {
	const root = baseEggSpecies(dex, species);
	const slotOrder = ["0", "1", "H", "S"];
	return {
		species: species.name,
		baseSpecies: species.baseSpecies || species.name,
		baseEggSpecies: root.name,
		eggGroups: species.eggGroups || [],
		gender: species.gender || "",
		genderRatio: species.genderRatio || {},
		canHatch: Boolean(species.canHatch),
		bst: Number(species.bst || 0),
		abilities: slotOrder
			.filter(slot => species.abilities?.[slot])
			.map(slot => ({
				slot,
				name: species.abilities[slot],
				hidden: slot === "H",
				special: slot === "S",
			})),
		eggMoves: eggMoveEntries(dex, root),
		levelUpMoves: levelUpMoveEntries(dex, root, 100),
	};
}

function chooseMoves(entries) {
	if (!entries.length) return [];
	if (entries.length <= 4) return entries.map(entry => entry.name);
	if (entries.every(entry => entry.level === 1)) {
		return shuffle(entries).slice(0, 4).map(entry => entry.name);
	}
	return entries.slice(-4).map(entry => entry.name);
}

function abilityPool(species, allowHiddenAbility) {
	const pool = [];
	for (const [slot, name] of Object.entries(species.abilities || {})) {
		if (!name) continue;
		if (slot === "H" && !allowHiddenAbility) continue;
		pool.push(name);
	}
	return pool;
}

function chooseGender(species) {
	if (species.gender) return species.gender;
	if (!species.genderRatio) return "";
	const femaleRate = Number(species.genderRatio.F || 0);
	return Math.random() < femaleRate ? "F" : "M";
}

function randomIvs() {
	return {
		hp: Math.floor(Math.random() * 32),
		atk: Math.floor(Math.random() * 32),
		def: Math.floor(Math.random() * 32),
		spa: Math.floor(Math.random() * 32),
		spd: Math.floor(Math.random() * 32),
		spe: Math.floor(Math.random() * 32),
	};
}

function randomInt(min, max) {
	min = Math.ceil(Number(min));
	max = Math.floor(Number(max));
	if (max <= min) return min;
	return Math.floor(Math.random() * (max - min + 1)) + min;
}

function weightedPick(options) {
	const totalWeight = options.reduce((sum, option) => sum + Number(option.weight || 0), 0);
	let roll = Math.random() * totalWeight;
	for (const option of options) {
		roll -= Number(option.weight || 0);
		if (roll <= 0) return option;
	}
	return options[options.length - 1];
}

function specialIvClass(species) {
	const tags = Array.isArray(species?.tags) ? species.tags : [];
	if (tags.includes("Mythical")) return "mythical";
	if (tags.includes("Ultra Beast")) return "ultra_beast";
	if (tags.includes("Restricted Legendary") || tags.includes("Sub-Legendary")) return "legendary";
	return "normal";
}

function parseIvProfile(profile) {
	const text = String(profile || "").trim().toLowerCase();
	const [tag, rawCount] = text.split(":", 2);
	const catchIndex = Number.parseInt(rawCount || "0", 10) || 0;
	if (!tag) {
		return {source: "wild", boosted: false, catchIndex};
	}
	if (tag === "hunt-boost") {
		return {source: "hunt", boosted: true, catchIndex};
	}
	if (tag === "hunt") {
		return {source: "hunt", boosted: false, catchIndex};
	}
	return {source: tag, boosted: false, catchIndex};
}

function ivPlanForEncounter(species, profile) {
	const parsed = parseIvProfile(profile);
	const speciesClass = specialIvClass(species);
	if (parsed.source === "safari") {
		return {
			source: parsed.source,
			boosted: parsed.boosted,
			min: 95,
			max: 160,
			floor: 2,
			lowChance: 0.003,
			lowMin: 32,
			lowMax: 39,
			highChance: 0.04,
			highMin: 162,
			highMax: 180,
			boostMin: 165,
			boostMax: 180,
		};
	}
	if (parsed.source === "starter") {
		return {
			source: parsed.source,
			boosted: parsed.boosted,
			min: 130,
			max: 168,
			floor: 6,
			lowChance: 0.001,
			lowMin: 36,
			lowMax: 39,
			highChance: 0.08,
			highMin: 169,
			highMax: 180,
			boostMin: 172,
			boostMax: 180,
		};
	}
	if (speciesClass === "mythical") {
		return {
			source: parsed.source,
			boosted: parsed.boosted,
			min: 92,
			max: 166,
			floor: 3,
			lowChance: 0.002,
			lowMin: 34,
			lowMax: 39,
			highChance: 0.12,
			highMin: 167,
			highMax: 180,
			boostMin: 170,
			boostMax: 180,
		};
	}
	if (speciesClass === "ultra_beast") {
		return {
			source: parsed.source,
			boosted: parsed.boosted,
			min: 88,
			max: 162,
			floor: 3,
			lowChance: 0.003,
			lowMin: 33,
			lowMax: 39,
			highChance: 0.1,
			highMin: 164,
			highMax: 180,
			boostMin: 168,
			boostMax: 180,
		};
	}
	if (speciesClass === "legendary") {
		return {
			source: parsed.source,
			boosted: parsed.boosted,
			min: 82,
			max: 160,
			floor: 2,
			lowChance: 0.004,
			lowMin: 32,
			lowMax: 39,
			highChance: 0.09,
			highMin: 162,
			highMax: 180,
			boostMin: 166,
			boostMax: 180,
		};
	}
	return {
		source: parsed.source,
		boosted: parsed.boosted,
		min: 45,
		max: 142,
		floor: 0,
		lowChance: 0.008,
		lowMin: 28,
		lowMax: 39,
		highChance: 0.015,
		highMin: 155,
		highMax: 180,
		boostMin: 140,
		boostMax: 180,
	};
}

function rollEncounterIvs(species, profile) {
	const parsed = parseIvProfile(profile);
	const plan = ivPlanForEncounter(species, profile);
	let targetMin = Number(plan.min || 0);
	let targetMax = Number(plan.max || 186);

	// Make very low totals possible but uncommon.
	const rolledLow = Math.random() < Number(plan.lowChance || 0);
	if (rolledLow) {
		targetMin = Number(plan.lowMin || 20);
		targetMax = Number(plan.lowMax || 39);
	}

	// Hunt jackpot: total IV > 180 appears only at 1/100,000 hunts.
	if (!rolledLow) {
		if (parsed.source === "hunt" && Math.random() < (1 / 100000)) {
			targetMin = 181;
			targetMax = 186;
		} else if (Math.random() < Number(plan.highChance || 0)) {
			targetMin = Number(plan.highMin || targetMin);
			targetMax = Number(plan.highMax || targetMax);
		}
	}

	if (plan.boosted) {
		targetMin = Math.max(targetMin, Number(plan.boostMin || targetMin));
		targetMax = Math.max(targetMin, Math.min(180, Number(plan.boostMax || targetMax)));
	}

	const stats = ["hp", "atk", "def", "spa", "spd", "spe"];
	const shuffledStats = shuffle(stats);
	const minTotal = Math.max(Number(plan.floor || 0) * stats.length, targetMin);
	const maxTotal = Math.min(31 * stats.length, targetMax);
	const targetTotal = randomInt(minTotal, maxTotal);
	const ivs = {};
	let remaining = targetTotal;

	for (let index = 0; index < shuffledStats.length; index += 1) {
		const stat = shuffledStats[index];
		const remainingSlots = shuffledStats.length - index - 1;
		const floor = Number(plan.floor || 0);
		const minAllowed = index === shuffledStats.length - 1
			? remaining
			: Math.max(floor, remaining - (remainingSlots * 31));
		const maxAllowed = index === shuffledStats.length - 1
			? remaining
			: Math.min(31, remaining - (remainingSlots * floor));
		const value = randomInt(minAllowed, maxAllowed);
		ivs[stat] = value;
		remaining -= value;
	}

	return ivs;
}

function defaultEvs(legalMinEvs = true) {
	return legalMinEvs ? {hp: 1, atk: 0, def: 0, spa: 0, spd: 0, spe: 0} : {hp: 0, atk: 0, def: 0, spa: 0, spd: 0, spe: 0};
}

function natureMultiplier(natureName, stat) {
	const [plus, minus] = NATURES[natureName] || [null, null];
	if (plus === stat) return 1.1;
	if (minus === stat) return 0.9;
	return 1;
}

function calculateStats(species, level, ivs, evs, natureName) {
	const stats = {};
	for (const stat of ["hp", "atk", "def", "spa", "spd", "spe"]) {
		const base = Number(species.baseStats?.[stat] || 0);
		const iv = Number(ivs?.[stat] || 0);
		const ev = Number(evs?.[stat] || 0);
		if (stat === "hp") {
			if (base === 1) {
				stats.hp = 1;
				continue;
			}
			stats.hp = Math.floor(((2 * base + iv + Math.floor(ev / 4)) * level) / 100) + level + 10;
			continue;
		}
		const raw = Math.floor(((2 * base + iv + Math.floor(ev / 4)) * level) / 100) + 5;
		stats[stat] = Math.floor(raw * natureMultiplier(natureName, stat));
	}
	return stats;
}

function generatePokemon(payload) {
	const formatid = String(payload.formatid || "gen9nationaldexag");
	const mod = String(payload.mod || "gen9");
	const dex = getDex(mod);
	const species = resolveSpecies(dex, payload.species);

	if (!species.exists) {
		fail(`Unknown species: ${String(payload.species || "")}`);
	}
	if (species.isNonstandard && species.isNonstandard !== "Past") {
		fail(`${species.name} is not available for this generator.`);
	}

	const level = Math.max(1, Math.min(100, Number(payload.level || 5)));
	const ivs = payload.ivs || rollEncounterIvs(species, payload.ivProfile);
	const evs = payload.evs || defaultEvs(payload.legalMinEvs !== false);
	const nature = String(payload.nature || sample(Object.keys(NATURES)));
	const abilities = abilityPool(species, Boolean(payload.allowHiddenAbility));
	if (!abilities.length) {
		fail(`${species.name} does not have a usable ability pool.`);
	}
	const requestedAbility = String(payload.ability || "").trim();
	const ability = abilities.includes(requestedAbility) ? requestedAbility : sample(abilities);
	const requestedGender = String(payload.gender || "").trim();
	const gender = String(species.gender || requestedGender || chooseGender(species));
	const friendship = Math.max(0, Math.min(255, Number(payload.friendship ?? 255)));
	const levelMoves = levelUpMoveEntries(dex, species, level);
	let moves = payload.moves || chooseMoves(levelMoves);

	if (!moves.length) {
		const fallback = [...dex.species.getMovePool(species.id, formatid.includes("nationaldex"))]
			.map(moveid => dex.moves.get(moveid))
			.filter(move => move.exists && !move.isNonstandard)
			.slice(0, 4)
			.map(move => move.name);
		moves = fallback;
	}
	if (!moves.length) {
		fail(`Could not build a movepool for ${species.name}.`);
	}

	const set = {
		name: payload.nickname ? String(payload.nickname) : species.name,
		species: species.name,
		gender,
		item: String(payload.item || ""),
		ability,
		moves,
		nature,
		evs,
		ivs,
		shiny: Boolean(payload.shiny),
		level,
		happiness: friendship,
		teraType: String(payload.teraType || species.types[0] || "Normal"),
	};

	const stats = calculateStats(species, level, ivs, evs, nature);
	const problems = TeamValidator.get(formatid).validateTeam([set]) || [];

	emit({
		ok: true,
		formatid,
		species: species.name,
		types: species.types || [],
		ability,
		abilityPool: abilities,
		level,
		gender,
		friendship,
		nature,
		item: set.item,
		teraType: set.teraType,
		ivs,
		evs,
		moves,
		levelUpMoves: levelMoves,
		stats,
		currentHp: stats.hp,
		maxHp: stats.hp,
		totalIv: Object.values(ivs).reduce((total, value) => total + Number(value || 0), 0),
		exportText: Teams.export([set]).trim(),
		packedTeam: Teams.pack([set]),
		problems,
	});
}

function listStarters() {
	emit({
		ok: true,
		regions: readJson(path.resolve(__dirname, "..", "game", "data", "starters.json")).regions,
	});
}

function getLevelUpMoves(payload) {
	const mod = String(payload.mod || "gen9");
	const dex = getDex(mod);
	const species = resolveSpecies(dex, payload.species);

	if (!species.exists) {
		fail(`Unknown species: ${String(payload.species || "")}`);
	}

	const level = Math.max(1, Math.min(100, Number(payload.level || 5)));
	const allMoves = levelUpMoveEntries(dex, species, level);
	const newMoves = allMoves.filter(move => move.level === level).map(move => move.name);

	emit({
		ok: true,
		species: species.name,
		level: level,
		moves: newMoves,
	});
}

function listHeldItems(payload) {
	const mod = String(payload.mod || "gen9");
	const dex = getDex(mod);
	const allowedNonstandard = new Set(["Past", "Future"]);
	const items = dex.items.all()
		.filter(item => (
			item.exists &&
			item.name &&
			!item.isPokeball &&
			(!item.isNonstandard || allowedNonstandard.has(item.isNonstandard) || item.megaStone)
		))
		.map(item => item.name)
		.sort((left, right) => left.localeCompare(right));

	emit({
		ok: true,
		items,
	});
}

function listTrainingMoves(payload) {
	const mod = String(payload.mod || "gen9");
	const dex = getDex(mod);
	const species = resolveSpecies(dex, payload.species);

	if (!species.exists) {
		fail(`Unknown species: ${String(payload.species || "")}`);
	}

	emit({
		ok: true,
		species: species.name,
		moves: trainingMoveEntries(dex, species),
	});
}

function listAbilities(payload) {
	const mod = String(payload.mod || "gen9");
	const dex = getDex(mod);
	const species = resolveSpecies(dex, payload.species);

	if (!species.exists) {
		fail(`Unknown species: ${String(payload.species || "")}`);
	}

	const slotOrder = ["0", "1", "H", "S"];
	const abilities = slotOrder
		.filter(slot => species.abilities?.[slot])
		.map(slot => ({
			slot,
			name: species.abilities[slot],
			hidden: slot === "H",
			special: slot === "S",
		}));

	emit({
		ok: true,
		species: species.name,
		abilities,
	});
}

function getBreedingProfile(payload) {
	const mod = String(payload.mod || "gen9");
	const dex = getDex(mod);
	const species = resolveSpecies(dex, payload.species);

	if (!species.exists) {
		fail(`Unknown species: ${String(payload.species || "")}`);
	}

	emit({
		ok: true,
		...breedingProfilePayload(dex, species),
	});
}

function getBreedingProfiles(payload) {
	const mod = String(payload.mod || "gen9");
	const dex = getDex(mod);
	const requested = Array.isArray(payload.speciesList) ? payload.speciesList : [];
	const profiles = requested.map(name => {
		const species = resolveSpecies(dex, name);
		if (!species.exists) {
			fail(`Unknown species: ${String(name || "")}`);
		}
		return breedingProfilePayload(dex, species);
	});
	emit({
		ok: true,
		profiles,
	});
}

function listEggSpecies(payload) {
	const mod = String(payload.mod || "gen9");
	const dex = getDex(mod);
	const allowedNonstandard = new Set(["Past", "Future"]);
	const species = dex.species.all()
		.filter(entry => (
			entry.exists &&
			entry.name &&
			!entry.forme &&
			!entry.battleOnly &&
			entry.baseSpecies === entry.name &&
			(!entry.isNonstandard || allowedNonstandard.has(entry.isNonstandard))
		))
		.map(entry => entry.name)
		.sort((left, right) => left.localeCompare(right));

	emit({
		ok: true,
		species,
	});
}

function handlePayload(payload) {
	switch (payload.type) {
	case "generate-pokemon":
		generatePokemon(payload);
		break;
	case "list-starters":
		listStarters();
		break;
	case "get-levelup-moves":
		getLevelUpMoves(payload);
		break;
	case "list-held-items":
		listHeldItems(payload);
		break;
	case "list-training-moves":
		listTrainingMoves(payload);
		break;
	case "list-abilities":
		listAbilities(payload);
		break;
	case "get-breeding-profile":
		getBreedingProfile(payload);
		break;
	case "get-breeding-profiles":
		getBreedingProfiles(payload);
		break;
	case "list-egg-species":
		listEggSpecies(payload);
		break;
	default:
		fail(`Unsupported dex helper command: ${String(payload.type || "")}`);
	}
}

const rl = readline.createInterface({
	input: process.stdin,
	crlfDelay: Infinity,
	terminal: false,
});

rl.on("line", line => {
	const text = String(line || "").trim();
	if (!text) return;
	try {
		const payload = parsePayload(text);
		if (!payload || typeof payload !== "object") {
			fail("Invalid dex helper payload.");
		}
		handlePayload(payload);
	} catch (error) {
		if (error instanceof DexToolError) {
			emitError(error.message);
			return;
		}
		emitError(`Unexpected Dex helper error: ${error?.message || String(error)}`);
	}
});
