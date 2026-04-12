function registerPokechainCommands(bot, deps) {
  const {
    sendMessage,
    getUserData,
    saveUserData2,
    c,
    chains,
    forms,
    toBaseIdentifier,
    moment
  } = deps;
  const fs = require('fs');
  const path = require('path');

  const GAME_MAP = new Map();
  const TURN_TIMERS = new Map();
  const LOBBY_TIMERS = new Map();
  let cachedAllowed = null;
  let cachedLineMap = null;

  function getGameKey(chatId) {
    return String(chatId);
  }

  function normalizeGuess(raw) {
    const base = String(raw || '').toLowerCase().trim();
    if (!base) return '';
    const cleaned = base.replace(/[\s_]+/g, '-').replace(/[^a-z0-9-]/g, '');
    if (toBaseIdentifier) {
      return toBaseIdentifier(cleaned, forms);
    }
    return cleaned;
  }

  function computeTimePerTurn(guessCount) {
    const step = Math.floor(Number(guessCount || 0) / 5);
    return Math.max(20, 45 - (step * 5));
  }

  function loadAllowedList() {
    if (cachedAllowed) return cachedAllowed;
    let names = [];
    try {
      const filePath = path.join(process.cwd(), 'data', 'pokechain.json');
      if (fs.existsSync(filePath)) {
        const raw = JSON.parse(fs.readFileSync(filePath, 'utf8'));
        if (Array.isArray(raw)) names = raw;
        else if (raw && Array.isArray(raw.names)) names = raw.names;
      }
    } catch (error) {
      names = [];
    }
    if (!names.length && forms && typeof forms === 'object') {
      names = Object.keys(forms);
    }
    cachedAllowed = new Set(names.map((n) => String(n).toLowerCase()));
    return cachedAllowed;
  }

  function buildEvolutionLineMap() {
    if (cachedLineMap) return cachedLineMap;
    const edges = new Map();
    const rows = (chains && chains.evolution_chains) ? chains.evolution_chains : [];
    for (const row of rows) {
      const from = String(row.current_pokemon || '').toLowerCase();
      const to = String(row.evolved_pokemon || '').toLowerCase();
      if (!from || !to) continue;
      if (!edges.has(from)) edges.set(from, new Set());
      if (!edges.has(to)) edges.set(to, new Set());
      edges.get(from).add(to);
      edges.get(to).add(from);
    }

    const lineMap = new Map();
    let lineId = 1;
    for (const node of edges.keys()) {
      if (lineMap.has(node)) continue;
      const queue = [node];
      lineMap.set(node, String(lineId));
      while (queue.length) {
        const current = queue.shift();
        const neighbors = edges.get(current) || new Set();
        for (const next of neighbors) {
          if (!lineMap.has(next)) {
            lineMap.set(next, String(lineId));
            queue.push(next);
          }
        }
      }
      lineId += 1;
    }
    cachedLineMap = lineMap;
    return cachedLineMap;
  }

  function getLineId(name) {
    const map = buildEvolutionLineMap();
    return map.get(name) || name;
  }

  function clearTurnTimer(chatId) {
    const key = getGameKey(chatId);
    const timer = TURN_TIMERS.get(key);
    if (timer) clearTimeout(timer);
    TURN_TIMERS.delete(key);
  }

  function clearLobbyTimer(chatId) {
    const key = getGameKey(chatId);
    const timer = LOBBY_TIMERS.get(key);
    if (timer) clearTimeout(timer);
    LOBBY_TIMERS.delete(key);
  }

  function setLobbyTimer(game) {
    clearLobbyTimer(game.chatId);
    const key = getGameKey(game.chatId);
    LOBBY_TIMERS.set(key, setTimeout(() => {
      handleLobbyTimeout(game.chatId).catch((error) => {
        console.error('Pokechain lobby timeout error:', error);
      });
    }, 120000));
  }

  function setTurnTimer(game, onTimeout) {
    clearTurnTimer(game.chatId);
    const key = getGameKey(game.chatId);
    const ms = Math.max(1, Number(game.timePerTurn || 45)) * 1000;
    game.deadline = Date.now() + ms;
    TURN_TIMERS.set(key, setTimeout(() => onTimeout(game.chatId), ms));
  }

  function getCtxForGame(game) {
    if (game && game.lastCtx) return game.lastCtx;
    return { chat: { id: game.chatId }, from: { id: game.hostId } };
  }

  function buildPlayerMention(userId, userData) {
    const name = userData && userData.inv && userData.inv.name ? userData.inv.name : String(userId);
    return `<a href="tg://user?id=${userId}"><b>${name}</b></a>`;
  }

  async function announceTurn(ctx, game) {
    const currentId = game.players[game.turnIndex];
    const playerData = await getUserData(currentId);
    const playerTag = buildPlayerMention(currentId, playerData);
    const time = game.timePerTurn;
    let msg = `Pokechain turn: ${playerTag}\nTime: <b>${time}s</b>`;
    msg += `\nStart with any Pokemon name.`;
    const mid = await sendMessage(ctx, game.chatId, { parse_mode: 'HTML' }, msg);
    if (mid) game.lastPromptId = mid;
  }

  async function finishGame(ctx, game, winnerId) {
    clearTurnTimer(game.chatId);
    clearLobbyTimer(game.chatId);
    GAME_MAP.delete(getGameKey(game.chatId));
    const winnerData = await getUserData(winnerId);
    const winnerTag = buildPlayerMention(winnerId, winnerData);

    let rewardMsg = 'Rewards: +1000 PokeCoins, +100 League Points';
    let rewarded = false;
    if (!winnerData.inv || typeof winnerData.inv !== 'object') winnerData.inv = {};
    if (!winnerData.extra || typeof winnerData.extra !== 'object') winnerData.extra = {};
    const today = moment ? moment().format('YYYY-MM-DD') : new Date().toISOString().slice(0, 10);
    if (!winnerData.extra.pokechain_rewards) {
      winnerData.extra.pokechain_rewards = { date: today, count: 0 };
    }
    const rewardData = winnerData.extra.pokechain_rewards;
    if (rewardData.date !== today) {
      rewardData.date = today;
      rewardData.count = 0;
    }
    if (rewardData.count >= 5) {
      rewardMsg = 'Reward limit reached for today (5/5).';
    } else {
      if (!Number.isFinite(winnerData.inv.pc)) winnerData.inv.pc = 0;
      if (!Number.isFinite(winnerData.inv.league_points)) winnerData.inv.league_points = 0;
      winnerData.inv.pc += 1000;
      winnerData.inv.league_points += 100;
      rewardData.count += 1;
      rewarded = true;
      await saveUserData2(winnerId, winnerData);
    }
    if (!rewarded) {
      await saveUserData2(winnerId, winnerData);
    }
    const msg = `Pokechain winner: ${winnerTag}\n${rewardMsg}`;
    await sendMessage(ctx, game.chatId, { parse_mode: 'HTML' }, msg);
  }

  async function eliminatePlayer(ctx, game, userId, reason) {
    const idx = game.players.indexOf(userId);
    if (idx === -1) return;
    const userData = await getUserData(userId);
    const userTag = buildPlayerMention(userId, userData);
    game.players.splice(idx, 1);

    let msg = `${userTag} is out. ${reason || ''}`.trim();
    if (game.players.length === 0) {
      clearTurnTimer(game.chatId);
      clearLobbyTimer(game.chatId);
      GAME_MAP.delete(getGameKey(game.chatId));
      await sendMessage(ctx, game.chatId, { parse_mode: 'HTML' }, msg + '\nNo winner this time.');
      return;
    }
    if (game.players.length === 1) {
      await sendMessage(ctx, game.chatId, { parse_mode: 'HTML' }, msg);
      await finishGame(ctx, game, game.players[0]);
      return;
    }
    if (idx < game.turnIndex) {
      game.turnIndex -= 1;
    }
    if (game.turnIndex >= game.players.length) {
      game.turnIndex = 0;
    }
    await sendMessage(ctx, game.chatId, { parse_mode: 'HTML' }, msg);
    setTurnTimer(game, handleTimeout);
    await announceTurn(ctx, game);
  }

  async function handleTimeout(chatId) {
    const game = GAME_MAP.get(getGameKey(chatId));
    if (!game || game.status !== 'active') return;
    const currentId = game.players[game.turnIndex];
    const ctx = getCtxForGame(game);
    await eliminatePlayer(ctx, game, currentId, 'Time is up.');
  }

  async function handleLobbyTimeout(chatId) {
    const game = GAME_MAP.get(getGameKey(chatId));
    if (!game || game.status !== 'lobby') return;
    const ctx = getCtxForGame(game);
    if (game.players.length < 2) {
      clearLobbyTimer(chatId);
      GAME_MAP.delete(getGameKey(chatId));
      await sendMessage(ctx, chatId, { parse_mode: 'markdown' }, '*Pokechain lobby closed. Need at least 2 players to auto-start.*');
      return;
    }
    await sendMessage(ctx, chatId, { parse_mode: 'markdown' }, '*Pokechain auto-started after 120 seconds.*');
    await startGame(ctx, game);
  }

  async function startGame(ctx, game) {
    if (game.players.length < 2) {
      await sendMessage(ctx, game.chatId, { parse_mode: 'markdown' }, '*Need at least 2 players to start.*');
      return;
    }
    clearLobbyTimer(game.chatId);
    game.status = 'active';
    game.turnIndex = 0;
    game.lastName = '';
    game.usedNames = [];
    game.usedLines = [];
    game.guessCount = 0;
    game.timePerTurn = computeTimePerTurn(0);
    setTurnTimer(game, handleTimeout);
    const startMid = await sendMessage(ctx, game.chatId, { parse_mode: 'HTML' }, 'Pokechain started. Use Pokemon names only.');
    if (startMid) game.lastPromptId = startMid;
    await announceTurn(ctx, game);
  }

  bot.command('pokechain', async (ctx) => {
    if (!ctx.chat || ctx.chat.type === 'private') {
      await sendMessage(ctx, ctx.chat.id, { parse_mode: 'markdown' }, '*Use this in a group chat.*');
      return;
    }
    const chatId = ctx.chat.id;
    const key = getGameKey(chatId);
    const args = String(ctx.message.text || '').split(' ').slice(1).map((v) => v.toLowerCase());
    const game = GAME_MAP.get(key);
    if (game) game.lastCtx = ctx;
    if (args[0] === 'end' || args[0] === 'stop') {
      if (!game) {
        await sendMessage(ctx, chatId, { parse_mode: 'markdown' }, '*No active Pokechain game.*');
        return;
      }
      clearTurnTimer(chatId);
      clearLobbyTimer(chatId);
      GAME_MAP.delete(key);
      await sendMessage(ctx, chatId, { parse_mode: 'markdown' }, '*Pokechain game ended.*');
      return;
    }
    if (args[0] === 'start') {
      if (!game) {
        await sendMessage(ctx, chatId, { parse_mode: 'markdown' }, '*No lobby found. Use /pokechain to create one.*');
        return;
      }
      if (game.status === 'active') {
        await sendMessage(ctx, chatId, { parse_mode: 'markdown' }, '*Pokechain already running.*');
        return;
      }
      if (String(game.hostId) !== String(ctx.from.id)) {
        await sendMessage(ctx, chatId, { parse_mode: 'markdown' }, '*Only the host can start the game.*');
        return;
      }
      await startGame(ctx, game);
      return;
    }

    if (game) {
      await sendMessage(ctx, chatId, { parse_mode: 'markdown' }, '*Pokechain already running in this chat.*');
      return;
    }
    const newGame = {
      chatId,
      hostId: ctx.from.id,
      status: 'lobby',
      players: [ctx.from.id],
      turnIndex: 0,
      lastName: '',
      usedNames: [],
      usedLines: [],
      guessCount: 0,
      timePerTurn: 45,
      createdAt: Date.now(),
      lastCtx: ctx
    };
    GAME_MAP.set(key, newGame);
    setLobbyTimer(newGame);
    const hostData = await getUserData(ctx.from.id);
    const hostTag = buildPlayerMention(ctx.from.id, hostData);
    const msg = `Pokechain lobby created by ${hostTag}\nUse /joinpc to join.\nHost: use /pokechain start when ready.\nAuto-start in 120 seconds.`;
    await sendMessage(ctx, chatId, { parse_mode: 'HTML' }, msg);
  });

  bot.command('end', async (ctx) => {
    if (!ctx.chat || ctx.chat.type === 'private') {
      await sendMessage(ctx, ctx.chat.id, { parse_mode: 'markdown' }, '*Use this in a group chat.*');
      return;
    }
    const key = getGameKey(ctx.chat.id);
    const game = GAME_MAP.get(key);
    if (!game) {
      await sendMessage(ctx, ctx.chat.id, { parse_mode: 'markdown' }, '*No active Pokechain game in this chat.*');
      return;
    }

    try {
      const member = await bot.telegram.getChatMember(ctx.chat.id, ctx.from.id);
      if (!member || !['administrator', 'creator'].includes(String(member.status))) {
        await sendMessage(ctx, ctx.chat.id, { parse_mode: 'markdown' }, '*Only group admins can use /end for Pokechain.*');
        return;
      }
    } catch (error) {
      await sendMessage(ctx, ctx.chat.id, { parse_mode: 'markdown' }, '*Could not verify admin status.*');
      return;
    }

    clearTurnTimer(ctx.chat.id);
    clearLobbyTimer(ctx.chat.id);
    GAME_MAP.delete(key);
    await sendMessage(ctx, ctx.chat.id, { parse_mode: 'markdown' }, '*Pokechain game ended by group admin.*');
  });

  bot.command('joinpc', async (ctx) => {
    if (!ctx.chat || ctx.chat.type === 'private') {
      await sendMessage(ctx, ctx.chat.id, { parse_mode: 'markdown' }, '*Use this in a group chat.*');
      return;
    }
    const chatId = ctx.chat.id;
    const key = getGameKey(chatId);
    const game = GAME_MAP.get(key);
    if (!game) {
      await sendMessage(ctx, chatId, { parse_mode: 'markdown' }, '*No Pokechain lobby. Use /pokechain to start.*');
      return;
    }
    game.lastCtx = ctx;
    if (game.status !== 'lobby') {
      await sendMessage(ctx, chatId, { parse_mode: 'markdown' }, '*Pokechain already started.*');
      return;
    }
    if (game.players.includes(ctx.from.id)) {
      await sendMessage(ctx, chatId, { parse_mode: 'markdown' }, '*You already joined.*');
      return;
    }
    game.players.push(ctx.from.id);
    const playerData = await getUserData(ctx.from.id);
    const playerTag = buildPlayerMention(ctx.from.id, playerData);
    const msg = `${playerTag} joined the lobby. Players: ${game.players.length}`;
    await sendMessage(ctx, chatId, { parse_mode: 'HTML' }, msg);
  });

  bot.on('text', async (ctx, next) => {
    try {
      if (!ctx.chat) return next();
      if (!ctx.message || !ctx.message.text) return next();
      if (ctx.message.text.startsWith('/')) return next();
      const key = getGameKey(ctx.chat.id);
      const game = GAME_MAP.get(key);
      if (!game || game.status !== 'active') return next();
      game.lastCtx = ctx;
      const currentId = game.players[game.turnIndex];
      if (String(ctx.from.id) !== String(currentId)) return next();

      const allowed = loadAllowedList();
      const guessRaw = ctx.message.text;
      const guess = normalizeGuess(guessRaw);
      if (!guess) return next();

      if (!allowed.has(guess)) {
        await sendMessage(ctx, game.chatId, { parse_mode: 'markdown' }, '*Invalid Pokemon name. Try again.*');
        return next();
      }
      if (game.usedNames.includes(guess)) {
        await sendMessage(ctx, game.chatId, { parse_mode: 'markdown' }, '*Already used. Try another.*');
        return next();
      }
      const lineId = getLineId(guess);
      if (game.usedLines.includes(lineId)) {
        await sendMessage(ctx, game.chatId, { parse_mode: 'markdown' }, '*Evolution line already used. Try another.*');
        return next();
      }

      game.usedNames.push(guess);
      game.usedLines.push(lineId);
      game.lastName = guess;
      game.guessCount += 1;
      game.timePerTurn = computeTimePerTurn(game.guessCount);
      game.turnIndex = (game.turnIndex + 1) % game.players.length;
      setTurnTimer(game, handleTimeout);

      const nextId = game.players[game.turnIndex];
      const nextData = await getUserData(nextId);
      const nextTag = buildPlayerMention(nextId, nextData);
      const msg = `Correct: <b>${c(guess)}</b>\nNext: ${nextTag}\nTime: <b>${game.timePerTurn}s</b>`;
      const mid = await sendMessage(ctx, game.chatId, { parse_mode: 'HTML' }, msg);
      if (mid) game.lastPromptId = mid;
    } catch (error) {
      console.error('Pokechain error:', error);
    }
    return next();
  });
}

module.exports = registerPokechainCommands;
