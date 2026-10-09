// Local Telegram Bot API mock for the SHMAX Playwright suite.
//
// The audit demands the E2E suite exercise PRODUCTION application
// components and mock ONLY the external services. This server is
// the external Telegram Bot API boundary: aiogram talks to it
// through SHMAX_TELEGRAM_API_BASE, so the real adapter, the real
// polling listener and the real pairing handler all run their
// genuine code paths against it.
//
// Test-only endpoint: POST /inject queues a raw Telegram update
// that the next getUpdates long-poll returns (drives the real
// receive path).

import http from 'node:http';

const BOT_TOKEN_PREFIX_OK = ':';

export class MockBotApi {
  constructor({ log = () => {} } = {}) {
    this.log = log;
    this.updates = []; // queued getUpdates results
    this.updateOffset = 1;
    this.sentMessages = []; // everything SHMAX "sends" via Telegram
    this.topics = {};
    // Facts getChat / getChatMember return — a healthy
    // supergroup with topics enabled by default. The objects
    // carry every field aiogram's ChatFullInfo /
    // ChatMemberAdministrator models require.
    this.chats = {
      '-1001234567890': {
        id: -1001234567890,
        type: 'supergroup',
        title: 'SHMAX E2E Group',
        is_forum: true,
        accent_color_id: 0,
        max_reaction_count: 100,
        accepted_gift_types: {
          unlimited_gifts: true,
          limited_gifts: true,
          unique_gifts: true,
          premium_subscription: true,
          gifts_from_channels: true,
        },
      },
    };
    this.botMember = {
      status: 'administrator',
      can_send_messages: true,
      can_manage_topics: true,
      is_member: true,
    };
    this.server = null;
    this.port = null;
  }

  get base() {
    return `http://127.0.0.1:${this.port}`;
  }

  /** Queue a Telegram update the way the real servers would deliver it. */
  injectUpdate(update) {
    this.updates.push({ update_id: this.updateOffset++, ...update });
  }

  setChat(chatId, facts) {
    this.chats[chatId] = { id: Number(chatId), ...facts };
  }

  async start() {
    const state = this;
    this.server = http.createServer((req, res) => {
      const url = new URL(req.url, 'http://localhost');
      const chunks = [];
      req.on('data', (c) => chunks.push(c));
      req.on('end', () => {
        // aiogram POSTs Bot API methods as form-urlencoded bodies
        // (getUpdates?offset=...&timeout=...) — parse both shapes.
        const raw = Buffer.concat(chunks).toString('utf8') || '';
        const ctype = (req.headers['content-type'] || '').toLowerCase();
        let body = {};
        try {
          if (raw && ctype.includes('json')) {
            body = JSON.parse(raw);
          } else if (raw) {
            body = Object.fromEntries(new URLSearchParams(raw).entries());
          }
        } catch {
          body = {};
        }
        // JSON-encoded lists (allowed_updates=[]) arrive URL-encoded;
        // decode nested JSON scalars where possible.
        for (const [k, v] of Object.entries(body)) {
          if (typeof v === 'string' && v.startsWith('[')) {
            try { body[k] = JSON.parse(v); } catch { /* keep string */ }
          }
        }
        if (process.env.E2E_VERBOSE) {
          console.log(`[mock] ${req.method} ${url.pathname}`);
        }
        const path = url.pathname;
        // Test-only subscription endpoint: returns a single VLESS
        // link pointing the stub xray at THIS mock (the e2e stub
        // forwards everything to SHMAX_E2E_FORWARD anyway).
        if (req.method === 'GET' && path === '/subscription') {
          const link = `vless://e2e-uuid@127.0.0.1:${state.port}?type=tcp&security=tls&sni=mock#E2E%20Mock%20Node`;
          res.writeHead(200, { 'content-type': 'application/text' });
          res.end(link);
          return;
        }
        // Test-only injection endpoint. Accepts either the raw
        // update object or {"update": {...}}.
        if (req.method === 'POST' && path === '/inject') {
          state.injectUpdate(body.update || body);
          res.writeHead(200, { 'content-type': 'application/json' });
          res.end(JSON.stringify({ ok: true }));
          return;
        }
        const method = path.split('/').pop();
        const reply = (payload) => {
          res.writeHead(200, { 'content-type': 'application/json' });
          res.end(JSON.stringify(payload));
        };
        const replyErr = (description, code = 400) => {
          res.writeHead(200, { 'content-type': 'application/json' });
          res.end(JSON.stringify({ ok: false, error_code: code, description }));
        };

        switch (method) {
          case 'getMe':
            return reply({
              ok: true,
              result: {
                id: 424242,
                is_bot: true,
                first_name: 'SHMAX E2E Bot',
                username: 'shmax_e2e_bot',
                can_join_groups: true,
              },
            });
          case 'getUpdates': {
            const pending = state.updates.splice(0, state.updates.length);
            if (pending.length === 0) {
              // Long poll — reply shortly so polling stays warm
              // but does not spin the CPU.
              setTimeout(() => reply({ ok: true, result: [] }), 150);
              return;
            }
            return reply({ ok: true, result: pending });
          }
          case 'sendMessage': {
            state.sentMessages.push({ kind: 'message', ...body });
            return reply({
              ok: true,
              result: { message_id: 7000 + state.sentMessages.length, chat: { id: Number(body.chat_id) }, date: Math.floor(Date.now() / 1000), text: body.text },
            });
          }
          case 'createForumTopic': {
            const topicId = 900 + Object.keys(state.topics).length;
            state.topics[topicId] = { ...body };
            // ForumTopicCreated requires message_thread_id + name.
            return reply({
              ok: true,
              result: {
                message_thread_id: topicId,
                name: body.name || 'topic',
                icon_color: 7322096,
              },
            });
          }
          case 'getChat': {
            const chatId = body.chat_id ?? url.searchParams.get('chat_id');
            const chat = state.chats[String(chatId)];
            if (!chat) return replyErr('chat not found', 400);
            return reply({ ok: true, result: chat });
          }
          case 'getChatMember': {
            const chatId = body.chat_id ?? url.searchParams.get('chat_id');
            const chat = state.chats[String(chatId)];
            if (!chat) return replyErr('chat not found', 400);
            const user = { id: 424242, is_bot: true, first_name: 'SHMAX E2E Bot', username: 'shmax_e2e_bot' };
            const result =
              state.botMember.status === 'administrator'
                ? {
                    status: 'administrator',
                    user,
                    can_be_edited: false,
                    is_anonymous: false,
                    can_manage_chat: true,
                    can_delete_messages: true,
                    can_manage_video_chats: true,
                    can_restrict_members: true,
                    can_promote_members: false,
                    can_change_info: true,
                    can_invite_users: true,
                    can_post_stories: false,
                    can_edit_stories: false,
                    can_delete_stories: false,
                    can_send_welcome_messages: false,
                    can_manage_topics: state.botMember.can_manage_topics,
                    can_send_messages: state.botMember.can_send_messages,
                  }
                : {
                    status: state.botMember.status,
                    user,
                    ...(state.botMember.status === 'restricted'
                      ? { is_member: state.botMember.is_member, can_send_messages: state.botMember.can_send_messages }
                      : {}),
                  };
            return reply({ ok: true, result });
          }
          case 'deleteWebhook':
          case 'close':
          case 'logOut':
            return reply({ ok: true, result: true });
          default:
            state.log(`mock-bot-api: unhandled method ${method}`);
            return replyErr(`method ${method} not implemented in mock`, 404);
        }
      });
    });
    await new Promise((resolve) => this.server.listen(0, '127.0.0.1', resolve));
    this.port = this.server.address().port;
    return this;
  }

  async stop() {
    if (this.server) {
      await new Promise((resolve) => this.server.close(resolve));
      this.server = null;
    }
  }
}
