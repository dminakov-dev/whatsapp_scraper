// Recorder (RECORD_DIR): saves what whatsapp-web.js really returned, in the fixture format
// fake-wa.js reads, so the same contract test can run on real data.
// The output is RAW (members' ids, message text, `_data` internals): it goes to a gitignored
// directory and is committed only after review and redaction.
import { mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';

import { chatIdOf, isCollectable } from './filter.js';

// whatsapp-web.js objects hold a reference to the Client (and through it the puppeteer page).
function plain(obj) {
  return JSON.parse(JSON.stringify(obj, (key, value) => (key === 'client' ? undefined : value)));
}

export function createRecorder(dir, { log = () => {} } = {}) {
  mkdirSync(dir, { recursive: true });
  const file = path.join(dir, 'session.json');
  const data = { state: 'CONNECTED', recorded_at: new Date().toISOString(), chats: [], messages: {} };
  const chatIds = new Set();

  function addChat(chat) {
    const id = chat?.id?._serialized;
    // Direct chats are never recorded either (their lastMessage is a private conversation).
    if (!id || !isCollectable(id) || chatIds.has(id)) return;
    chatIds.add(id);
    data.chats.push(plain(chat));
  }

  function addMessages(chatId, msgs) {
    const list = (data.messages[chatId] ||= []);
    const seen = new Set(list.map((m) => m.id?._serialized));
    for (const m of msgs) {
      const p = plain(m);
      if (!seen.has(p.id?._serialized)) list.push(p);
    }
  }

  function flush() {
    writeFileSync(file, JSON.stringify(data, null, 2));
    log('info', 'recorded', { file, chats: data.chats.length });
  }

  return {
    chats(list) { list.forEach(addChat); flush(); },
    history(chatId, chat, msgs) { addChat(chat); addMessages(chatId, msgs); flush(); },
    message(msg) {
      const chatId = chatIdOf(msg);
      if (chatId && isCollectable(chatId)) { addMessages(chatId, [msg]); flush(); }
    },
  };
}
