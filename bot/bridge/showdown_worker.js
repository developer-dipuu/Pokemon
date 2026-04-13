"use strict";

const fs = require("node:fs");
const path = require("node:path");
const readline = require("node:readline");

function emit(type, payload = {}) {
	process.stdout.write(`${JSON.stringify({type, ...payload})}\n`);
}

function respond(requestId, payload = {}) {
	emit("response", {requestId, payload});
}

function respondError(requestId, error) {
	const message = error && error.stack ? error.stack : String(error);
	emit("response_error", {requestId, message});
}

function bridgeError(error) {
	const message = error && error.stack ? error.stack : String(error);
	emit("bridge_error", {message});
}

process.on("uncaughtException", error => {
	bridgeError(error);
	process.exit(1);
});

process.on("unhandledRejection", error => {
	bridgeError(error);
	process.exit(1);
});

const showdownDir = path.resolve(__dirname, "..", "..", "server", "pokemon-showdown");
const distEntry = path.join(showdownDir, "dist", "sim", "index.js");

if (!fs.existsSync(distEntry)) {
	emit("bridge_error", {
		message: `Pokemon Showdown is not built yet. Expected ${distEntry}. Run "node build" inside ${showdownDir}.`,
	});
	process.exit(1);
}

const {BattleStream, Dex, getPlayerStreams} = require(distEntry);

let battleStream = null;
let streams = null;
let battleEnded = false;
let closing = false;

function parseChunk(chunk) {
	return String(chunk)
		.split("\n")
		.map(line => line.trimEnd())
		.filter(Boolean);
}

function speciesName(details = "") {
	return String(details).split(",", 1)[0].trim();
}

function levelFromDetails(details = "") {
	const match = String(details).match(/\bL(\d+)\b/);
	return match ? match[1] : "?";
}

function enrichPokemon(pokemon) {
	const details = pokemon?.details || pokemon?.ident || "";
	const currentTypes = pokemon?.terastallized ? [pokemon.terastallized] : Dex.species.get(speciesName(details)).types;
	const item = Dex.items.get(pokemon?.item || "");
	return {
		...pokemon,
		displayName: speciesName(details),
		displayLevel: levelFromDetails(details),
		displayTypes: currentTypes || [],
		displayItem: item?.name || pokemon?.item || "",
	};
}

function enrichMove(move) {
	const moveData = Dex.moves.get(move?.id || move?.move || "");
	return {
		...move,
		displayType: moveData?.type || "?",
		displayAccuracy: moveData?.accuracy === true ? "--" : String(moveData?.accuracy ?? "?"),
	};
}

function enrichRequest(request) {
	const nextRequest = {...request};
	if (nextRequest.side?.pokemon) {
		nextRequest.side = {
			...nextRequest.side,
			pokemon: nextRequest.side.pokemon.map(enrichPokemon),
		};
	}
	if (nextRequest.active) {
		nextRequest.active = nextRequest.active.map(active => ({
			...active,
			moves: (active.moves || []).map(enrichMove),
		}));
	}
	return nextRequest;
}

function slotIndex(slot) {
	if (slot === "p1") return 0;
	if (slot === "p2") return 1;
	if (slot === "p3") return 2;
	if (slot === "p4") return 3;
	throw new Error(`Unknown battle slot: ${slot}`);
}

function safeInvoke(fn, fallback, notes) {
	try {
		return fn();
	} catch {
		notes.add("best-effort");
		return fallback;
	}
}

function safeTypeList(value) {
	if (!Array.isArray(value)) return [];
	return value.map(item => String(item)).filter(Boolean);
}

function stagedStat(base, stage) {
	const safeBase = Number(base || 0);
	const safeStage = Number(stage || 0);
	if (!safeBase || !safeStage) return safeBase;
	if (safeStage > 0) {
		return Math.floor((safeBase * (2 + safeStage)) / 2);
	}
	return Math.floor((safeBase * 2) / (2 + Math.abs(safeStage)));
}

function buildActiveStatsSnapshot(slot) {
	const battle = battleStream?.battle;
	if (!battle) {
		throw new Error("No active battle is running.");
	}
	const side = battle.sides[slotIndex(slot)];
	if (!side) {
		throw new Error(`Unknown battle side: ${slot}`);
	}
	const pokemon = side.active?.[0];
	if (!pokemon) {
		throw new Error("No active Pokemon is available for that side yet.");
	}

	const notes = new Set();
	const fallbackBaseTypes = safeTypeList(pokemon.species?.types || Dex.species.get(pokemon.species?.name || pokemon.name || "").types || []);
	const item = safeInvoke(() => pokemon.getItem(), Dex.items.get(pokemon?.item || ""), notes);
	const baseTypes = safeTypeList(safeInvoke(() => pokemon.getTypes(true, true), fallbackBaseTypes, notes));
	const currentTypes = safeTypeList(
		safeInvoke(
			() => pokemon.getTypes(true, false),
			pokemon.terastallized ? [String(pokemon.terastallized)] : baseTypes,
			notes
		)
	);
	const stats = {
		hp: {
			current: Number(pokemon.hp || 0),
			max: Number(pokemon.maxhp || 0),
		},
	};
	for (const stat of ["atk", "def", "spa", "spd", "spe"]) {
		const baseValue = Number(pokemon.storedStats?.[stat] || 0);
		const stageValue = Number(pokemon.boosts?.[stat] || 0);
		const unboostedValue = Number(safeInvoke(() => pokemon.getStat(stat, true, false), baseValue, notes) || baseValue);
		const currentFallback = stagedStat(unboostedValue || baseValue, stageValue);
		stats[stat] = {
			base: baseValue,
			unboosted: unboostedValue,
			current: Number(safeInvoke(() => pokemon.getStat(stat, false, false), currentFallback, notes) || currentFallback),
			stage: stageValue,
		};
	}

	return {
		slot,
		name: pokemon.species?.name || pokemon.name || side.name,
		level: Number(pokemon.level || 0),
		status: String(pokemon.status || "").toUpperCase(),
		baseTypes: baseTypes || [],
		currentTypes: currentTypes || [],
		teraType: String(pokemon.teraType || ""),
		terastallized: String(pokemon.terastallized || ""),
		item: item?.name || "",
		itemId: item?.id || "",
		itemShortDesc: item?.shortDesc || item?.desc || "",
		hp: stats.hp,
		stats,
		bestEffort: notes.size > 0,
	};
}

function markBattleEnded(payload) {
	if (battleEnded) return;
	battleEnded = true;
	emit("ended", payload);
}

async function listenPublic(stream) {
	try {
		for await (const chunk of stream) {
			const lines = parseChunk(chunk);
			if (!lines.length) continue;
			emit("public", {lines});

			for (const line of lines) {
				if (line.startsWith("|win|")) {
					markBattleEnded({winner: line.slice(5), tie: false});
				} else if (line === "|tie") {
					markBattleEnded({winner: null, tie: true});
				}
			}
		}
	} catch (error) {
		bridgeError(error);
	}
}

async function listenPlayer(slot, stream) {
	try {
		for await (const chunk of stream) {
			const lines = parseChunk(chunk);
			for (const line of lines) {
				if (line.startsWith("|request|")) {
					emit("request", {
						slot,
						request: enrichRequest(JSON.parse(line.slice(9))),
					});
				} else if (line.startsWith("|error|")) {
					emit("error", {
						slot,
						message: line.slice(7),
					});
				}
			}
		}
	} catch (error) {
		bridgeError(error);
	}
}

async function startBattle(payload) {
	if (battleStream) {
		throw new Error("Battle already started in this worker.");
	}

	battleEnded = false;
	battleStream = new BattleStream();
	streams = getPlayerStreams(battleStream);

	void listenPublic(streams.spectator);
	void listenPlayer("p1", streams.p1);
	void listenPlayer("p2", streams.p2);

	const spec = {
		formatid: payload.formatid || "gen9randombattle",
	};
	if (Array.isArray(payload.seed) && payload.seed.length) {
		spec.seed = payload.seed.map(value => Number(value) || 0);
	}
	const p1 = {
		name: payload.p1?.name || "Player 1",
	};
	const p2 = {
		name: payload.p2?.name || "Player 2",
	};
	if (payload.p1?.team) {
		p1.team = payload.p1.team;
	}
	if (payload.p2?.team) {
		p2.team = payload.p2.team;
	}

	await streams.omniscient.write(
		`>start ${JSON.stringify(spec)}\n` +
		`>player p1 ${JSON.stringify(p1)}\n` +
		`>player p2 ${JSON.stringify(p2)}`
	);

	emit("started", {
		formatid: spec.formatid,
		p1: p1.name,
		p2: p2.name,
	});
}

async function choose(payload) {
	if (!streams || !streams[payload.slot]) {
		throw new Error(`Unknown battle slot: ${payload.slot}`);
	}
	await streams[payload.slot].write(payload.choice);
}

async function undo(payload) {
	if (!streams || !streams[payload.slot]) {
		throw new Error(`Unknown battle slot: ${payload.slot}`);
	}
	await streams[payload.slot].write("undo");
}

async function forfeit(payload) {
	if (!battleStream) {
		throw new Error("No active battle to forfeit.");
	}
	await battleStream.write(`>forcelose ${payload.slot}`);
}

async function activeStats(payload) {
	if (!payload.requestId) {
		throw new Error("Missing requestId for active stat request.");
	}
	try {
		respond(payload.requestId, buildActiveStatsSnapshot(payload.slot));
	} catch (error) {
		respondError(payload.requestId, error);
	}
}

async function closeBridge() {
	if (closing) return;
	closing = true;

	try {
		if (battleStream) {
			battleStream.writeEnd();
		}
	} catch (error) {
		bridgeError(error);
	}

	setTimeout(() => process.exit(0), 25).unref();
}

async function handleMessage(message) {
	switch (message.type) {
	case "start":
		await startBattle(message);
		break;
	case "choose":
		await choose(message);
		break;
	case "undo":
		await undo(message);
		break;
	case "forfeit":
		await forfeit(message);
		break;
	case "active-stats":
		await activeStats(message);
		break;
	case "close":
		await closeBridge();
		break;
	default:
		throw new Error(`Unsupported command type: ${message.type}`);
	}
}

const lineReader = readline.createInterface({
	input: process.stdin,
	crlfDelay: Infinity,
});

let commandChain = Promise.resolve();

lineReader.on("line", line => {
	if (!line.trim()) return;

	commandChain = commandChain
		.then(async () => {
			let message;
			try {
				message = JSON.parse(line);
			} catch {
				throw new Error(`Invalid JSON command: ${line}`);
			}

			await handleMessage(message);
		})
		.catch(error => {
			bridgeError(error);
		});
});

lineReader.on("close", () => {
	void closeBridge();
});

emit("ready", {showdownDir});
