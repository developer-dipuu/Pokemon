const fs = require('fs');
const path = require('path');

// ─── Config ───────────────────────────────────────────────────────────────────
const ADMIN_IDS      = [6265981509];
const CHANNEL_ID     = -1003707195144;
const DEFAULT_PFP    = 'https://files.catbox.moe/5rg0pw.jpg';
const DATA_FILE      = path.join(process.cwd(), 'data', 'factions.json');

// ─── Logging ──────────────────────────────────────────────────────────────────
function log(label, extra) {
  const ts = new Date().toISOString();
  extra !== undefined
    ? console.log(`[${ts}]`, label, extra)
    : console.log(`[${ts}]`, label);
}

function registerFactionModule(bot, options = {}) {
  const botStartTime = Date.now();
  const botUsername = String(options.botUsername || bot.botInfo?.username || '').trim();

// ─── Pre-message timestamp guard ──────────────────────────────────────────────
bot.use(async (ctx, next) => {
  try {
    const ts =
      ctx.message?.date ??
      ctx.editedMessage?.date ??
      ctx.callbackQuery?.message?.date ??
      ctx.update?.message?.date ??
      ctx.update?.edited_message?.date ??
      ctx.update?.callback_query?.message?.date;

    if (ts && ts * 1000 < botStartTime) return;

    const text   = ctx.message?.text ?? '';
    const fromId = ctx.from?.id    ?? '';
    const chatId = ctx.chat?.id    ?? '';

    if (text.trim().startsWith('/')) {
      const cmd         = text.trim().split(' ')[0];
      const hasMention  = cmd.includes('@');
      const isForMe     = !hasMention || !botUsername || cmd.toLowerCase().includes('@' + botUsername.toLowerCase());
      if (!isForMe) return next();
      log('CMD', { fromId, chatId, text });
    }
  } catch (_) {}
  return next();
});

// ─── Data helpers ─────────────────────────────────────────────────────────────
let cachedData = { factions: [] };

function cloneJson(value) {
  if (value === undefined) return undefined;
  if (value === null) return null;
  try {
    return JSON.parse(JSON.stringify(value));
  } catch (_) {
    return value;
  }
}

async function loadDataFromStore() {
  try {
    if (!fs.existsSync(DATA_FILE)) {
      fs.mkdirSync(path.dirname(DATA_FILE), { recursive: true });
      fs.writeFileSync(DATA_FILE, JSON.stringify({ factions: [] }, null, 2), 'utf8');
    }
    const raw = fs.readFileSync(DATA_FILE, 'utf8');
    const data = JSON.parse(raw);
    cachedData = (data && Array.isArray(data.factions)) ? data : { factions: [] };
  } catch (_) {
    cachedData = { factions: [] };
  }
}

function loadData() {
  try {
    if (!fs.existsSync(DATA_FILE)) {
      fs.mkdirSync(path.dirname(DATA_FILE), { recursive: true });
      fs.writeFileSync(DATA_FILE, JSON.stringify({ factions: [] }, null, 2), 'utf8');
    }
    const raw = fs.readFileSync(DATA_FILE, 'utf8');
    const data = JSON.parse(raw);
    cachedData = (data && Array.isArray(data.factions)) ? data : { factions: [] };
  } catch (_) {
    cachedData = { factions: [] };
  }
  return cloneJson(cachedData);
}

function saveData(data) {
  cachedData = cloneJson(data && typeof data === 'object' ? data : { factions: [] });
  try {
    fs.mkdirSync(path.dirname(DATA_FILE), { recursive: true });
    fs.writeFileSync(DATA_FILE, JSON.stringify(cachedData, null, 2), 'utf8');
  } catch (error) {
    log('SAVE FAILED', { error: error?.message ?? String(error) });
  }
}

// ─── Formatting helpers ───────────────────────────────────────────────────────
function esc(text) {
  return String(text || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/** Clickable user mention */
function mention(userId, name) {
  return `<a href="tg://user?id=${userId}">${esc(name || userId)}</a>`;
}

/** Format a role badge */
function roleBadge(role) {
  const map = { captain: '👑 Captain', admin: '⚔️ Admin', member: '🛡️ Member' };
  return map[role] || role;
}

/** Divider line for messages */
const DIVIDER = '─────────────────────';

// ─── Faction finders ──────────────────────────────────────────────────────────
function findById(data, id)       { return data.factions.find(f => String(f.id)      === String(id)); }
function findByName(data, name)   { const n = name.trim().toLowerCase(); return data.factions.find(f => f.name.trim().toLowerCase() === n); }
function findByCaptain(data, uid) { return data.factions.find(f => String(f.captain) === String(uid)); }
function findByMember(data, uid)  { return data.factions.find(f => (f.members || []).includes(String(uid))); }
function inAnyFaction(data, uid)  { return findByMember(data, uid) || findByCaptain(data, uid); }

function ensureDefaults(faction) {
  if (!faction.pfp)                           faction.pfp         = DEFAULT_PFP;
  if (!Array.isArray(faction.members))        faction.members     = [];
  if (!Array.isArray(faction.admins))         faction.admins      = [];
  if (!faction.memberNames || typeof faction.memberNames !== 'object') faction.memberNames = {};
  if (!faction.inv || typeof faction.inv !== 'object') faction.inv = {};
  if (!Number.isFinite(faction.inv.vp)) faction.inv.vp = 0;
  if (Number.isFinite(faction.inv.pc) && faction.inv.pc !== 0) faction.inv.vp += Number(faction.inv.pc);
  if (Object.prototype.hasOwnProperty.call(faction.inv, 'pc')) delete faction.inv.pc;
}

function touchName(faction, user) {
  if (!user?.id) return;
  ensureDefaults(faction);
  faction.memberNames[String(user.id)] = user.first_name || user.username || String(user.id);
}

function getUserName(faction, uid) {
  return faction.memberNames?.[String(uid)] || String(uid);
}

// ─── Channel post builder ─────────────────────────────────────────────────────
function buildChannelPost(faction) {
  ensureDefaults(faction);
  const members   = faction.members || [];
  const admins    = faction.admins  || [];
  const captainId = String(faction.captain || '');
  const captainNm = getUserName(faction, captainId) || faction.captain_name || captainId;

  const adminLinks = admins.length
    ? admins.map(a => mention(a.userId, a.name || a.userId)).join(', ')
    : 'None';

  const memberList = members.length
    ? members.map((id, i) => `${i + 1}. ${mention(id, getUserName(faction, id))}`).join('\n')
    : 'No members yet.';

  return [
    `🏰 <b>${esc(faction.name)}</b>`,
    DIVIDER,
    `👑 <b>Captain:</b> ${mention(captainId, captainNm)}`,
    `⚔️ <b>Admins:</b> ${adminLinks}`,
    `🛡️ <b>Members:</b> ${members.length}`,
    DIVIDER,
    '<b>Member List</b>',
    memberList,
  ].join('\n');
}

async function syncChannel(data, faction) {
  const text = buildChannelPost(faction);
  const opts = { parse_mode: 'HTML', disable_web_page_preview: true };
  if (faction.channelMessageId) {
    try {
      await bot.telegram.editMessageText(CHANNEL_ID, faction.channelMessageId, null, text, opts);
      return;
    } catch (_) {}
  }
  const msg = await bot.telegram.sendMessage(CHANNEL_ID, text, opts);
  faction.channelMessageId = msg.message_id;
  saveData(data);
}

// ─── Auth helpers ─────────────────────────────────────────────────────────────
function isGlobalAdmin(ctx) { return ADMIN_IDS.includes(ctx.from.id); }

function requireGlobalAdmin(ctx) {
  if (!isGlobalAdmin(ctx)) {
    ctx.reply('🚫 <b>Access denied.</b>\nThis command is restricted to bot administrators.', { parse_mode: 'HTML' });
    return false;
  }
  return true;
}

function isCaptainOf(faction, uid)  { return String(faction.captain) === String(uid); }
function isAdminOf(faction, uid)    { return (faction.admins || []).some(a => String(a.userId) === String(uid)); }
function isOfficerOf(faction, uid)  { return isCaptainOf(faction, uid) || isAdminOf(faction, uid); }

// ─── DM redirect helper ───────────────────────────────────────────────────────
function dmButton() {
  return {
    parse_mode: 'HTML',
    reply_markup: {
      inline_keyboard: [[{ text: '💬 Open Bot DM', url: `https://t.me/${botUsername}` }]]
    }
  };
}

// ─── /start & /help ───────────────────────────────────────────────────────────
function helpText() {
  return [
    '📖 <b>Faction Bot — Command Reference</b>',
    DIVIDER,
    '<b>🔒 Admin Commands</b>',
    '/create <i>Name</i> <i>CaptainUID</i> — Create a new faction',
    '/deletefac <i>Name</i> — Delete a faction',
    '',
    '<b>👑 Captain Commands</b>',
    '/setgc — Set faction group (use inside the group)',
    '/setname <i>New Name</i> — Rename your faction',
    '/setpfp <i>URL</i> — Set faction profile picture',
    '/facpromote — Promote a member (reply to their message)',
    '/facdemote — Demote an admin (reply to their message)',
    '/kick_member <i>UID</i> — Kick a member (or reply)',
    '',
    '<b>🛡️ Member Commands</b>',
    '/myfac — View your faction info',
    '/join — Join a faction (DM only)',
    '/leave — Leave your faction (DM only)',
    '/fac_link — Get a faction invite link (DM only)',
  ].join('\n');
}

bot.command(['start', 'help'], ctx => ctx.reply(helpText(), { parse_mode: 'HTML' }));

// ─── /create ─────────────────────────────────────────────────────────────────
bot.command('create', async ctx => {
  log('CMD /create', { from: ctx.from.id });
  if (!requireGlobalAdmin(ctx)) return;

  const parts = ctx.message.text.split(' ').filter(Boolean);
  if (parts.length < 3) {
    return ctx.reply(
      '⚠️ <b>Usage:</b> /create <i>Faction Name</i> <i>CaptainUID</i>\n\n<i>Example:</i> /create Shadow Legion 123456789',
      { parse_mode: 'HTML' }
    );
  }

  const captainId = parts[parts.length - 1];
  if (isNaN(Number(captainId))) {
    return ctx.reply('❌ <b>Invalid UID.</b>\nCaptain UID must be a numeric Telegram user ID.', { parse_mode: 'HTML' });
  }

  const name = parts.slice(1, -1).join(' ').trim();
  if (!name) {
    return ctx.reply('❌ <b>Missing faction name.</b>', { parse_mode: 'HTML' });
  }

  const data = loadData();
  if (findByName(data, name)) {
    return ctx.reply(`❌ A faction named <b>${esc(name)}</b> already exists.`, { parse_mode: 'HTML' });
  }

  const id      = String(Date.now());
  const faction = {
    id, name,
    captain:          Number(captainId),
    captain_name:     '',
    members:          [String(captainId)],
    memberNames:      {},
    admins:           [],
    group:            null,
    pfp:              DEFAULT_PFP,
    channelMessageId: null,
    inv:              { vp: 0 },
  };

  data.factions.push(faction);
  saveData(data);
  await syncChannel(data, faction);

  log('CREATE', { name, captainId });
  ctx.reply(
    `✅ <b>Faction created!</b>\n\n🏰 <b>${esc(name)}</b>\n👑 Captain UID: <code>${captainId}</code>\n\nTell the captain to use /setgc in their faction group.`,
    { parse_mode: 'HTML' }
  );
});

// ─── /deletefac ──────────────────────────────────────────────────────────────
bot.command('deletefac', async ctx => {
  log('CMD /deletefac', { from: ctx.from.id });
  if (!requireGlobalAdmin(ctx)) return;

  const name = ctx.message.text.split(' ').slice(1).join(' ').trim();
  if (!name) {
    return ctx.reply('⚠️ <b>Usage:</b> /deletefac <i>Faction Name</i>', { parse_mode: 'HTML' });
  }

  const data    = loadData();
  const faction = findByName(data, name);
  if (!faction) {
    return ctx.reply(`❌ No faction named <b>${esc(name)}</b> found.`, { parse_mode: 'HTML' });
  }

  const cb = `facdel_${faction.id}_${ctx.from.id}`;
  ctx.reply(
    `⚠️ <b>Confirm Deletion</b>\n\nYou are about to permanently delete <b>${esc(faction.name)}</b> with ${faction.members?.length || 0} member(s).\n\n<i>This action cannot be undone.</i>`,
    {
      parse_mode: 'HTML',
      reply_markup: { inline_keyboard: [[{ text: '🗑️ Confirm Delete', callback_data: cb }]] }
    }
  );
});

bot.action(/facdel_/, async ctx => {
  const [, factionId, adminId] = ctx.callbackQuery.data.split('_');
  if (String(ctx.from.id) !== String(adminId) || !isGlobalAdmin(ctx)) {
    return ctx.answerCbQuery('🚫 Not authorized.');
  }

  const data    = loadData();
  const faction = findById(data, factionId);
  if (!faction) return ctx.answerCbQuery('❌ Faction not found.');

  const fName = faction.name;
  data.factions = data.factions.filter(f => f.id !== faction.id);
  saveData(data);

  if (faction.channelMessageId) {
    try { await bot.telegram.deleteMessage(CHANNEL_ID, faction.channelMessageId); } catch (_) {}
  }

  try {
    await ctx.editMessageText(
      `🗑️ <b>Faction deleted.</b>\n\n<b>${esc(fName)}</b> has been permanently removed.`,
      { parse_mode: 'HTML' }
    );
  } catch (_) {}
  ctx.answerCbQuery('Deleted.');
  log('DELETE', { name: fName });
});

// ─── /setgc ───────────────────────────────────────────────────────────────────
bot.command('setgc', async ctx => {
  log('CMD /setgc', { from: ctx.from.id, chat: ctx.chat?.id });
  if (!ctx.chat || ctx.chat.type === 'private') {
    return ctx.reply('⚠️ Use this command <b>inside the faction group</b> you want to set.', { parse_mode: 'HTML' });
  }

  const data    = loadData();
  const faction = findByCaptain(data, ctx.from.id);
  if (!faction) {
    return ctx.reply('❌ You are not a captain of any faction.', { parse_mode: 'HTML' });
  }

  const chatId = ctx.chat.id;
  try {
    const [userMember, botMember, me] = await Promise.all([
      bot.telegram.getChatMember(chatId, ctx.from.id),
      bot.telegram.getChatMember(chatId, (await bot.telegram.getMe()).id),
      bot.telegram.getMe(),
    ]);
    if (!['creator', 'administrator'].includes(userMember.status)) {
      return ctx.reply('❌ You must be a <b>group administrator</b> to link this group.', { parse_mode: 'HTML' });
    }
    if (!['creator', 'administrator'].includes(botMember.status)) {
      return ctx.reply('❌ I need to be an <b>administrator</b> in this group to function properly.', { parse_mode: 'HTML' });
    }
  } catch (_) {
    return ctx.reply('❌ Could not verify group permissions. Make sure I have the right access.', { parse_mode: 'HTML' });
  }

  faction.group = chatId;
  touchName(faction, ctx.from);
  saveData(data);
  await syncChannel(data, faction);

  log('SETGC', { faction: faction.name, group: chatId });
  ctx.reply(
    `✅ <b>Faction group set!</b>\n\n<b>${esc(faction.name)}</b> is now linked to this group.\nMembers can now use /join to request entry.`,
    { parse_mode: 'HTML' }
  );
});

// ─── /myfac ───────────────────────────────────────────────────────────────────
bot.command('myfac', async ctx => {
  log('CMD /myfac', { from: ctx.from.id });
  const data    = loadData();
  const faction = inAnyFaction(data, ctx.from.id);
  if (!faction) {
    return ctx.reply('❌ You are not in any faction.\n\nUse /join in DM to request membership.', { parse_mode: 'HTML' });
  }

  ensureDefaults(faction);
  touchName(faction, ctx.from);
  saveData(data);

  const captainId  = String(faction.captain || '');
  const captainNm  = getUserName(faction, captainId) || faction.captain_name || captainId;
  const admins     = (faction.admins || []).map(a => mention(a.userId, a.name || a.userId)).join(', ') || 'None';
  const memberCount = (faction.members || []).length;
  const bankVp = Number(faction.inv && faction.inv.vp) || 0;

  let roleLabel = '🛡️ Member';
  if (isCaptainOf(faction, ctx.from.id))    roleLabel = '👑 Captain';
  else if (isAdminOf(faction, ctx.from.id)) roleLabel = '⚔️ Admin';

  const caption = [
    `🏰 <b>${esc(faction.name)}</b>`,
    DIVIDER,
    `<b>Your role:</b> ${roleLabel}`,
    `<b>Captain:</b> ${mention(captainId, captainNm)}`,
    `<b>Admins:</b> ${admins}`,
    `<b>Members:</b> ${memberCount}`,
    `<b>Faction Victory Points:</b> ${bankVp}`,
  ].join('\n');

  await ctx.replyWithPhoto(faction.pfp || DEFAULT_PFP, { caption, parse_mode: 'HTML' });
});

bot.command('faclb', async ctx => {
  log('CMD /faclb', { from: ctx.from.id });
  const data = loadData();
  const factions = Array.isArray(data.factions) ? data.factions.slice() : [];

  if (!factions.length) {
    return ctx.reply('No factions found.', { parse_mode: 'HTML' });
  }

  factions.forEach(ensureDefaults);
  factions.sort((a, b) => {
    const aVp = Number(a.inv && a.inv.vp) || 0;
    const bVp = Number(b.inv && b.inv.vp) || 0;
    if (bVp !== aVp) return bVp - aVp;
    return String(a.name || '').localeCompare(String(b.name || ''));
  });

  const lines = [
    '<b>Faction Leaderboard</b>',
    DIVIDER
  ];

  factions.slice(0, 15).forEach((faction, index) => {
    const totalVp = Number(faction.inv && faction.inv.vp) || 0;
    lines.push(`${index + 1}. <b>${esc(faction.name)}</b> - <code>${totalVp}</code> VP`);
  });

  return ctx.reply(lines.join('\n'), { parse_mode: 'HTML' });
});

// ─── /join ────────────────────────────────────────────────────────────────────
bot.command('join', async ctx => {
  log('CMD /join', { from: ctx.from.id, chat: ctx.chat?.type });
  if (ctx.chat?.type !== 'private') {
    return ctx.reply('📬 <b>Use /join in a private DM with me.</b>', dmButton());
  }

  const data     = loadData();
  const existing = inAnyFaction(data, ctx.from.id);
  if (existing) {
    return ctx.reply(
      `⚠️ You are already a member of <b>${esc(existing.name)}</b>.\n\nUse /leave first if you want to join another faction.`,
      { parse_mode: 'HTML' }
    );
  }

  const factions = data.factions || [];
  if (factions.length === 0) {
    return ctx.reply('😔 <b>No factions available yet.</b>\nCheck back later!', { parse_mode: 'HTML' });
  }

  const rows = [];
  let row    = [];
  for (const f of factions) {
    row.push({ text: `🏰 ${f.name}`, callback_data: `facjoin_${f.id}` });
    if (row.length === 2) { rows.push(row); row = []; }
  }
  if (row.length) rows.push(row);

  ctx.reply(
    '🏰 <b>Choose a faction to join:</b>\n\nYour request will be sent to the faction officers for approval.',
    { parse_mode: 'HTML', reply_markup: { inline_keyboard: rows } }
  );
});

bot.action(/facjoin_/, async ctx => {
  const id       = String(ctx.callbackQuery.data.split('_')[1] || '');
  const data     = loadData();
  const existing = inAnyFaction(data, ctx.from.id);
  if (existing) {
    return ctx.answerCbQuery(`⚠️ Already in ${existing.name}.`);
  }

  const faction = findById(data, id);
  if (!faction) return ctx.answerCbQuery('❌ Faction not found.');

  ensureDefaults(faction);
  if (!faction.group) {
    return ctx.answerCbQuery('⚠️ This faction has not set up their group yet.');
  }

  touchName(faction, ctx.from);
  saveData(data);

  const userLink    = mention(ctx.from.id, ctx.from.first_name || ctx.from.username || String(ctx.from.id));
  const approveCb   = `facapp_${faction.id}_${ctx.from.id}`;
  const declineCb   = `facdec_${faction.id}_${ctx.from.id}`;

  await bot.telegram.sendMessage(
    faction.group,
    [
      `📩 <b>Join Request</b>`,
      DIVIDER,
      `${userLink} wants to join <b>${esc(faction.name)}</b>.`,
      '',
      '<i>Approve or decline below:</i>',
    ].join('\n'),
    {
      parse_mode:   'HTML',
      reply_markup: {
        inline_keyboard: [[
          { text: '✅ Approve', callback_data: approveCb },
          { text: '❌ Decline', callback_data: declineCb },
        ]]
      }
    }
  );

  await ctx.editMessageText(
    `✅ <b>Request sent to ${esc(faction.name)}!</b>\n\nWait for an officer to approve your request.`,
    { parse_mode: 'HTML' }
  );
  ctx.answerCbQuery('Request sent!');
});

// ─── Approve / Decline join ───────────────────────────────────────────────────
bot.action(/facapp_/, async ctx => {
  const [, factionId, userId] = ctx.callbackQuery.data.split('_');
  const data    = loadData();
  const faction = findById(data, factionId);

  if (!faction) return ctx.answerCbQuery('❌ Faction not found.');
  if (!isOfficerOf(faction, ctx.from.id)) return ctx.answerCbQuery('🚫 Not authorized.');

  if (inAnyFaction(data, userId)) {
    return ctx.answerCbQuery('⚠️ User is already in a faction.');
  }

  if (!faction.members.includes(String(userId))) faction.members.push(String(userId));
  saveData(data);
  await syncChannel(data, faction);

  const approverName = getUserName(faction, ctx.from.id) || ctx.from.first_name || String(ctx.from.id);
  const userName     = getUserName(faction, userId) || userId;

  // Notify the user
  try {
    let inviteText = `🎉 <b>Welcome to ${esc(faction.name)}!</b>\n\nYour request was approved by ${esc(approverName)}.`;
    if (faction.group) {
      try {
        const invite   = await bot.telegram.createChatInviteLink(faction.group, { limit: 1, expire_date: Math.floor(Date.now() / 1000) + 300 });
        inviteText    += `\n\n🔗 <b>Group invite link (5 min):</b>\n${invite.invite_link}`;
      } catch (_) {}
    }
    await bot.telegram.sendMessage(userId, inviteText, { parse_mode: 'HTML' });
  } catch (_) {}

  // Update the approval message
  try {
    await ctx.editMessageText(
      `✅ <b>Approved</b>\n\n${mention(userId, userName)} was approved by ${mention(ctx.from.id, approverName)} and added to <b>${esc(faction.name)}</b>.`,
      { parse_mode: 'HTML', disable_web_page_preview: true }
    );
  } catch (_) {}
  ctx.answerCbQuery('Approved!');
  log('APPROVE', { faction: faction.name, user: userId, by: ctx.from.id });
});

bot.action(/facdec_/, async ctx => {
  const [, factionId, userId] = ctx.callbackQuery.data.split('_');
  const data    = loadData();
  const faction = findById(data, factionId);

  if (!faction) return ctx.answerCbQuery('❌ Faction not found.');
  if (!isOfficerOf(faction, ctx.from.id)) return ctx.answerCbQuery('🚫 Not authorized.');

  const declinerName = getUserName(faction, ctx.from.id) || ctx.from.first_name || String(ctx.from.id);
  const userName     = getUserName(faction, userId) || userId;

  try {
    await bot.telegram.sendMessage(
      userId,
      `❌ <b>Request declined.</b>\n\nYour request to join <b>${esc(faction.name)}</b> was declined.`,
      { parse_mode: 'HTML' }
    );
  } catch (_) {}

  try {
    await ctx.editMessageText(
      `❌ <b>Declined</b>\n\n${mention(userId, userName)}'s request was declined by ${mention(ctx.from.id, declinerName)}.`,
      { parse_mode: 'HTML', disable_web_page_preview: true }
    );
  } catch (_) {}
  ctx.answerCbQuery('Declined.');
  log('DECLINE', { faction: faction.name, user: userId, by: ctx.from.id });
});

// ─── /leave ───────────────────────────────────────────────────────────────────
bot.command('leave', async ctx => {
  log('CMD /leave', { from: ctx.from.id });
  if (ctx.chat?.type !== 'private') {
    return ctx.reply('📬 <b>Use /leave in a private DM with me.</b>', dmButton());
  }

  const data    = loadData();
  const faction = inAnyFaction(data, ctx.from.id);
  if (!faction) {
    return ctx.reply('❌ You are not in any faction.', { parse_mode: 'HTML' });
  }
  if (isCaptainOf(faction, ctx.from.id)) {
    return ctx.reply(
      '👑 <b>Captains cannot leave their faction.</b>\n\nContact a bot admin if you need to transfer leadership.',
      { parse_mode: 'HTML' }
    );
  }

  const fName = faction.name;
  const uid   = String(ctx.from.id);

  faction.members  = faction.members.filter(id => id !== uid);
  faction.admins   = faction.admins.filter(a => String(a.userId) !== uid);
  ensureDefaults(faction);
  delete faction.memberNames[uid];
  saveData(data);
  await syncChannel(data, faction);

  if (faction.group) {
    try { await bot.telegram.kickChatMember(faction.group, ctx.from.id, { until_date: Math.floor(Date.now() / 1000) + 1 }); } catch (_) {}
  }

  log('LEAVE', { faction: fName, user: ctx.from.id });
  ctx.reply(
    `👋 <b>You left ${esc(fName)}.</b>\n\nYou can use /join anytime to join another faction.`,
    { parse_mode: 'HTML' }
  );
});

// ─── /fac_link ────────────────────────────────────────────────────────────────
bot.command('fac_link', async ctx => {
  log('CMD /fac_link', { from: ctx.from.id });
  if (ctx.chat?.type !== 'private') {
    return ctx.reply('📬 <b>Use /fac_link in a private DM with me.</b>', dmButton());
  }

  const data    = loadData();
  const faction = inAnyFaction(data, ctx.from.id);
  if (!faction?.group) {
    return ctx.reply('❌ Your faction does not have a group set up yet.', { parse_mode: 'HTML' });
  }

  try {
    const invite = await bot.telegram.createChatInviteLink(faction.group, { limit: 1, expire_date: Math.floor(Date.now() / 1000) + 30 });
    ctx.reply(
      `🔗 <b>${esc(faction.name)} — Invite Link</b>\n\n${invite.invite_link}\n\n<i>⏰ Expires in 30 seconds · 1 use only</i>`,
      { parse_mode: 'HTML', disable_web_page_preview: true }
    );
  } catch (_) {
    ctx.reply('❌ Failed to generate invite link.\nMake sure I am an administrator in the faction group.', { parse_mode: 'HTML' });
  }
});

// ─── /kick_member ─────────────────────────────────────────────────────────────
bot.command('kick_member', async ctx => {
  log('CMD /kick_member', { from: ctx.from.id });
  const data    = loadData();
  const faction = inAnyFaction(data, ctx.from.id);
  if (!faction) {
    return ctx.reply('❌ You are not in any faction.', { parse_mode: 'HTML' });
  }
  if (!faction.group || String(ctx.chat?.id) !== String(faction.group)) {
    return ctx.reply('⚠️ Use this command in your faction group.', { parse_mode: 'HTML' });
  }
  if (!isOfficerOf(faction, ctx.from.id)) {
    return ctx.reply('🚫 Only captains and admins can kick members.', { parse_mode: 'HTML' });
  }

  let targetId = null;
  if (ctx.message.reply_to_message) {
    targetId = ctx.message.reply_to_message.from.id;
  } else {
    const parts = ctx.message.text.split(' ').filter(Boolean);
    if (parts[1]) targetId = parts[1];
  }

  if (!targetId || isNaN(Number(targetId))) {
    return ctx.reply('⚠️ <b>Usage:</b> Reply to a member\'s message, or:\n/kick_member <i>UserUID</i>', { parse_mode: 'HTML' });
  }

  if (String(targetId) === String(faction.captain)) {
    return ctx.reply('👑 The captain cannot be kicked.', { parse_mode: 'HTML' });
  }
  if (String(targetId) === String(ctx.from.id)) {
    return ctx.reply('🤔 You cannot kick yourself. Use /leave instead.', { parse_mode: 'HTML' });
  }
  // Prevent admin kicking another admin (unless captain)
  if (!isCaptainOf(faction, ctx.from.id) && isAdminOf(faction, String(targetId))) {
    return ctx.reply('🚫 Admins cannot kick other admins. Only the captain can.', { parse_mode: 'HTML' });
  }
  if (!faction.members.includes(String(targetId))) {
    return ctx.reply('❌ That user is not in your faction.', { parse_mode: 'HTML' });
  }

  const targetName = getUserName(faction, targetId);
  const kickerName = getUserName(faction, ctx.from.id) || ctx.from.first_name || String(ctx.from.id);

  faction.members = faction.members.filter(id => String(id) !== String(targetId));
  faction.admins  = faction.admins.filter(a => String(a.userId) !== String(targetId));
  ensureDefaults(faction);
  delete faction.memberNames[String(targetId)];
  saveData(data);
  await syncChannel(data, faction);

  try { await bot.telegram.kickChatMember(faction.group, Number(targetId), { until_date: Math.floor(Date.now() / 1000) + 1 }); } catch (_) {}

  // Notify kicked user
  try {
    await bot.telegram.sendMessage(
      targetId,
      `🚪 You were removed from <b>${esc(faction.name)}</b> by ${esc(kickerName)}.`,
      { parse_mode: 'HTML' }
    );
  } catch (_) {}

  log('KICK', { faction: faction.name, target: targetId, by: ctx.from.id });
  ctx.reply(
    `🚪 <b>Member removed.</b>\n\n${mention(targetId, targetName)} was kicked from <b>${esc(faction.name)}</b> by ${mention(ctx.from.id, kickerName)}.`,
    { parse_mode: 'HTML', disable_web_page_preview: true }
  );
});

// ─── /facpromote ─────────────────────────────────────────────────────────────
bot.command('facpromote', async ctx => {
  log('CMD /facpromote', { from: ctx.from.id });
  const data    = loadData();
  const faction = findByCaptain(data, ctx.from.id);
  if (!faction) {
    return ctx.reply('👑 Only the <b>faction captain</b> can promote members.', { parse_mode: 'HTML' });
  }
  if (!ctx.message.reply_to_message) {
    return ctx.reply('⚠️ <b>Reply to a member\'s message</b> to promote them.', { parse_mode: 'HTML' });
  }

  const target   = ctx.message.reply_to_message.from;
  const targetId = target.id;

  if (String(targetId) === String(faction.captain)) {
    return ctx.reply('👑 That user is already the captain.', { parse_mode: 'HTML' });
  }
  if (!faction.members.includes(String(targetId))) {
    return ctx.reply('❌ That user is not a member of your faction.', { parse_mode: 'HTML' });
  }
  if (isAdminOf(faction, targetId)) {
    return ctx.reply(
      `⚔️ ${mention(targetId, getUserName(faction, targetId))} is already an admin.`,
      { parse_mode: 'HTML', disable_web_page_preview: true }
    );
  }

  const targetName = target.first_name || target.username || String(targetId);
  faction.admins.push({ userId: String(targetId), name: targetName });
  touchName(faction, target);
  saveData(data);
  await syncChannel(data, faction);

  log('PROMOTE', { faction: faction.name, target: targetId, by: ctx.from.id });
  ctx.reply(
    `⚔️ <b>Promoted!</b>\n\n${mention(targetId, targetName)} is now an <b>Admin</b> of <b>${esc(faction.name)}</b>.`,
    { parse_mode: 'HTML', disable_web_page_preview: true }
  );
});

// ─── /facdemote ──────────────────────────────────────────────────────────────
bot.command('facdemote', async ctx => {
  log('CMD /facdemote', { from: ctx.from.id });
  const data    = loadData();
  const faction = findByCaptain(data, ctx.from.id);
  if (!faction) {
    return ctx.reply('👑 Only the <b>faction captain</b> can demote admins.', { parse_mode: 'HTML' });
  }
  if (!ctx.message.reply_to_message) {
    return ctx.reply('⚠️ <b>Reply to a member\'s message</b> to demote them.', { parse_mode: 'HTML' });
  }

  const target   = ctx.message.reply_to_message.from;
  const targetId = target.id;

  if (!isAdminOf(faction, targetId)) {
    return ctx.reply(
      `❌ ${mention(targetId, getUserName(faction, targetId) || target.first_name || String(targetId))} is not an admin.`,
      { parse_mode: 'HTML', disable_web_page_preview: true }
    );
  }

  const targetName = getUserName(faction, targetId) || target.first_name || String(targetId);
  faction.admins   = faction.admins.filter(a => String(a.userId) !== String(targetId));
  saveData(data);
  await syncChannel(data, faction);

  log('DEMOTE', { faction: faction.name, target: targetId, by: ctx.from.id });
  ctx.reply(
    `🛡️ <b>Demoted.</b>\n\n${mention(targetId, targetName)} is now a regular <b>Member</b> of <b>${esc(faction.name)}</b>.`,
    { parse_mode: 'HTML', disable_web_page_preview: true }
  );
});

// ─── /setpfp — captain submits, global admins approve ────────────────────────
bot.command('setpfp', async ctx => {
  log('CMD /setpfp', { from: ctx.from.id });
  const data    = loadData();
  const faction = findByCaptain(data, ctx.from.id);
  if (!faction) {
    return ctx.reply('👑 Only the <b>faction captain</b> can request a photo change.', { parse_mode: 'HTML' });
  }

  const parts = ctx.message.text.split(' ').filter(Boolean);
  if (parts.length < 2) {
    return ctx.reply('⚠️ <b>Usage:</b> /setpfp <i>https://image-url.com/image.jpg</i>', { parse_mode: 'HTML' });
  }

  const url = parts[1].trim();
  if (!/^https?:\/\/.+/.test(url)) {
    return ctx.reply('❌ <b>Invalid URL.</b>\nMust start with <code>https://</code> or <code>http://</code>', { parse_mode: 'HTML' });
  }

  const captainId   = String(ctx.from.id);
  const captainName = ctx.from.first_name || ctx.from.username || captainId;

  // Store pending pfp on the faction so the approve handler can read it
  ensureDefaults(faction);
  faction.pendingPfp = url;
  saveData(data);

  // Encode url safely as base64 to avoid underscore conflicts in callback_data
  const urlB64     = Buffer.from(url).toString('base64').replace(/=/g, '');
  // callback_data limit is 64 bytes — store pending in faction data instead and
  // just pass faction id + captain id in the callback
  const approveCb  = `pfpapp_${faction.id}_${captainId}`;
  const declineCb  = `pfpdec_${faction.id}_${captainId}`;

  // Notify every global admin
  for (const adminId of ADMIN_IDS) {
    try {
      await bot.telegram.sendPhoto(
        adminId,
        url,
        {
          caption: [
            `🖼️ <b>PFP Change Request</b>`,
            DIVIDER,
            `<b>Faction:</b> ${esc(faction.name)}`,
            `<b>Captain:</b> ${mention(captainId, captainName)}`,
            `<b>New image URL:</b> <code>${esc(url)}</code>`,
            '',
            '<i>Approve or decline this photo below:</i>',
          ].join('\n'),
          parse_mode: 'HTML',
          reply_markup: {
            inline_keyboard: [[
              { text: '✅ Approve', callback_data: approveCb },
              { text: '❌ Decline', callback_data: declineCb },
            ]]
          }
        }
      );
    } catch (_) {
      // Admin may not have started the bot — skip silently
    }
  }

  log('SETPFP_REQUEST', { faction: faction.name, captain: captainId, url });
  ctx.reply(
    [
      `📤 <b>Photo request submitted!</b>`,
      '',
      `Your new image for <b>${esc(faction.name)}</b> has been sent to the admins for review.`,
      `You'll be notified once it's approved or declined.`,
    ].join('\n'),
    { parse_mode: 'HTML' }
  );
});

// ─── PFP approve ─────────────────────────────────────────────────────────────
bot.action(/pfpapp_/, async ctx => {
  if (!isGlobalAdmin(ctx)) return ctx.answerCbQuery('🚫 Not authorized.');

  const [, factionId, captainId] = ctx.callbackQuery.data.split('_');
  const data    = loadData();
  const faction = findById(data, factionId);

  if (!faction) return ctx.answerCbQuery('❌ Faction not found.');
  if (!faction.pendingPfp) return ctx.answerCbQuery('⚠️ No pending photo for this faction.');

  const approvedUrl    = faction.pendingPfp;
  const captainName    = getUserName(faction, captainId) || captainId;
  const adminName      = ctx.from.first_name || ctx.from.username || String(ctx.from.id);

  faction.pfp          = approvedUrl;
  faction.pendingPfp   = null;
  touchName(faction, ctx.from);
  saveData(data);
  await syncChannel(data, faction);

  // Update the admin's approval message
  try {
    await ctx.editMessageCaption(
      [
        `✅ <b>PFP Approved</b>`,
        DIVIDER,
        `<b>Faction:</b> ${esc(faction.name)}`,
        `<b>Approved by:</b> ${mention(ctx.from.id, adminName)}`,
        `<b>Captain:</b> ${mention(captainId, captainName)}`,
      ].join('\n'),
      { parse_mode: 'HTML', disable_web_page_preview: true }
    );
  } catch (_) {}

  // Notify the captain
  try {
    await bot.telegram.sendPhoto(
      captainId,
      approvedUrl,
      {
        caption: [
          `✅ <b>Photo approved!</b>`,
          '',
          `Your new profile picture for <b>${esc(faction.name)}</b> was approved by an admin and is now live.`,
        ].join('\n'),
        parse_mode: 'HTML',
      }
    );
  } catch (_) {}

  ctx.answerCbQuery('✅ Approved!');
  log('SETPFP_APPROVED', { faction: faction.name, by: ctx.from.id });
});

// ─── PFP decline ─────────────────────────────────────────────────────────────
bot.action(/pfpdec_/, async ctx => {
  if (!isGlobalAdmin(ctx)) return ctx.answerCbQuery('🚫 Not authorized.');

  const [, factionId, captainId] = ctx.callbackQuery.data.split('_');
  const data    = loadData();
  const faction = findById(data, factionId);

  if (!faction) return ctx.answerCbQuery('❌ Faction not found.');

  const captainName  = getUserName(faction, captainId) || captainId;
  const adminName    = ctx.from.first_name || ctx.from.username || String(ctx.from.id);
  const declinedUrl  = faction.pendingPfp || '(unknown)';

  faction.pendingPfp = null;
  saveData(data);

  // Update the admin's message
  try {
    await ctx.editMessageCaption(
      [
        `❌ <b>PFP Declined</b>`,
        DIVIDER,
        `<b>Faction:</b> ${esc(faction.name)}`,
        `<b>Declined by:</b> ${mention(ctx.from.id, adminName)}`,
        `<b>Captain:</b> ${mention(captainId, captainName)}`,
      ].join('\n'),
      { parse_mode: 'HTML', disable_web_page_preview: true }
    );
  } catch (_) {}

  // Notify the captain
  try {
    await bot.telegram.sendMessage(
      captainId,
      [
        `❌ <b>Photo declined.</b>`,
        '',
        `Your requested profile picture for <b>${esc(faction.name)}</b> was declined by an admin.`,
        `Please try a different image with /setpfp.`,
      ].join('\n'),
      { parse_mode: 'HTML' }
    );
  } catch (_) {}

  ctx.answerCbQuery('❌ Declined.');
  log('SETPFP_DECLINED', { faction: faction.name, by: ctx.from.id });
});

// ─── /setname ─────────────────────────────────────────────────────────────────
bot.command('setname', async ctx => {
  log('CMD /setname', { from: ctx.from.id });
  const data    = loadData();
  const faction = findByCaptain(data, ctx.from.id);
  if (!faction) {
    return ctx.reply('👑 Only the <b>faction captain</b> can rename the faction.', { parse_mode: 'HTML' });
  }

  const newName = ctx.message.text.split(' ').slice(1).join(' ').trim();
  if (!newName) {
    return ctx.reply('⚠️ <b>Usage:</b> /setname <i>New Faction Name</i>', { parse_mode: 'HTML' });
  }
  if (newName.length > 32) {
    return ctx.reply('❌ Faction name must be <b>32 characters or fewer</b>.', { parse_mode: 'HTML' });
  }

  const existing = findByName(data, newName);
  if (existing && existing.id !== faction.id) {
    return ctx.reply(`❌ Another faction is already named <b>${esc(newName)}</b>.`, { parse_mode: 'HTML' });
  }

  const oldName    = faction.name;
  faction.name     = newName;
  saveData(data);
  await syncChannel(data, faction);

  log('SETNAME', { old: oldName, new: newName });
  ctx.reply(
    `✏️ <b>Faction renamed!</b>\n\n<b>${esc(oldName)}</b> → <b>${esc(newName)}</b>`,
    { parse_mode: 'HTML' }
  );
});

// ─── Passive name tracking ────────────────────────────────────────────────────
bot.on('message', async (ctx, next) => {
  const data    = loadData();
  const faction = inAnyFaction(data, ctx.from.id);
  if (faction) {
    touchName(faction, ctx.from);
    saveData(data);
  }
  return next();
});

// ─── Launch ───────────────────────────────────────────────────────────────────
return loadDataFromStore().catch((error) => {
  log('FACTION DATA LOAD FAILED', { error: error?.message ?? String(error) });
});
}

module.exports = registerFactionModule;
