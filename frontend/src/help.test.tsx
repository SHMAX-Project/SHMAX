// Live acceptance addendum #6 — help / onboarding content tests.
//
// Verifies: wizard help cards render, Telegram Bot instructions
// mention BotFather + /setprivacy, Group instructions mention
// Topics + admin + /shmax_connect, Xray instructions exist, and
// the Help page renders all sections (routing is covered by the
// Playwright suite).

import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import React from 'react';
import '@testing-library/jest-dom/vitest';

import { HelpPage, WIZARD_HELP, WizardHelpCard, TROUBLESHOOTING } from './help';

describe('wizard help cards', () => {
  it('every wizard step except admin-lite has help content', () => {
    for (const id of ['admin', 'max', 'telegram-network', 'telegram-bot', 'telegram-group', 'max-chats', 'users', 'test', 'finish'] as const) {
      expect(WIZARD_HELP[id], `help for ${id}`).toBeTruthy();
    }
  });

  it('bot instructions mention BotFather and /setprivacy', () => {
    const { container } = render(<WizardHelpCard stepId="telegram-bot" />);
    const text = container.textContent ?? '';
    expect(text).toContain('BotFather');
    expect(text).toContain('/setprivacy');
    expect(text).toContain('token');
  });

  it('group instructions mention Topics, admin and /shmax_connect', () => {
    const { container } = render(<WizardHelpCard stepId="telegram-group" />);
    const text = container.textContent ?? '';
    expect(text).toContain('Темы');
    expect(text).toContain('администратор');
    expect(text).toContain('/shmax_connect');
  });

  it('network instructions cover Direct, proxy and Xray steps', () => {
    const { container } = render(<WizardHelpCard stepId="telegram-network" />);
    const text = container.textContent ?? '';
    expect(text).toContain('Direct');
    expect(text).toContain('SOCKS5');
    expect(text).toContain('Xray');
    expect(text).toContain('Получить подписку');
    // No credentials in the instructions.
    expect(text.toLowerCase()).not.toContain('password');
  });

  it('MAX instructions warn about 2FA and resend spam', () => {
    const { container } = render(<WizardHelpCard stepId="max" />);
    const text = container.textContent ?? '';
    expect(text).toContain('2FA');
    expect(text).toContain('не нажимайте');
  });

  it('troubleshooting lists the canonical cases', () => {
    const flat = TROUBLESHOOTING.map(([q]) => q).join(' ');
    expect(flat).toContain('SMS MAX');
    expect(flat).toContain('2FA');
    expect(flat).toContain('Группа не подключается');
    expect(flat).toContain('/shmax_connect');
  });
});

describe('help page', () => {
  it('renders quick start, all sections and troubleshooting', () => {
    render(<HelpPage />);
    expect(screen.getByTestId('help-page')).toBeInTheDocument();
    expect(screen.getByText('Быстрый старт')).toBeInTheDocument();
    for (const id of ['max', 'telegram-network', 'telegram-bot', 'telegram-group', 'max-chats', 'users', 'backups', 'updates', 'diagnostics']) {
      expect(screen.getByTestId(`help-section-${id}`)).toBeInTheDocument();
    }
    expect(screen.getByTestId('troubleshooting')).toBeInTheDocument();
  });
});


describe('current operator guidance', () => {
  it('explains optional bot discovery separately from authorization', () => {
    const { container } = render(<WizardHelpCard stepId="users" />);
    const text = container.textContent ?? '';
    for (const term of ['молчавших', 'API ID', 'API Hash', 'my.telegram.org', 'без userbot', 'не одобрены', 'Telegram admin не даёт SHMAX admin', 'chat_member', 'Ошибка синхронизации не останавливает мост']) {
      expect(text).toContain(term);
    }
  });

  it('includes Finish and explains the limits of Privacy Mode verification and backups', () => {
    const { container } = render(<HelpPage />);
    const text = container.textContent ?? '';
    expect(text).toContain('пользователи → проверка → готово');
    expect(text).toContain('getMe не подтвердил отключение');
    expect(text).toContain('не доставку сообщений группы');
    expect(text).toContain('содержит мастер-ключ');
    expect(text).toContain('может быть незашифрованным');
    expect(text).toContain('один зашифрованный .shmax');
    expect(text).toContain('Остальные шаги мастера не нужны');
    expect(text).not.toContain('Кнопка «Проверить сейчас»');
  });
});
