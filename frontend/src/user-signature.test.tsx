import React from 'react';
import { beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { UserSignature } from './user-signature';
import { api } from './api';
vi.mock('./api', () => ({ api: vi.fn() }));
const mock = vi.mocked(api);
beforeEach(() => { cleanup(); mock.mockReset(); });
const user = { user_id: '42', nickname: 'Роман', display_name: 'Ромка 💀 Шалапай 😈', username: 'quostiid' };

it('loads the stored nickname and updates the preview and saves only nickname', async () => {
  const onSaved = vi.fn(); mock.mockResolvedValue({ nickname: 'Анна 😈' });
  render(<UserSignature user={user} onSaved={onSaved} />);
  const field = screen.getByLabelText('Подпись в MAX');
  expect(field).toHaveValue('Роман');
  fireEvent.change(field, { target: { value: '  Анна 😈  ' } });
  expect(screen.getByText('Предпросмотр: [Анна 😈]: Текст сообщения')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Сохранить подпись' }));
  await waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
  expect(mock).toHaveBeenCalledWith('/api/telegram/users/42/nickname', { method: 'PATCH', body: JSON.stringify({ nickname: 'Анна 😈' }) });
});

it('clears the stored override and shows the Telegram-name fallback', async () => {
  mock.mockResolvedValue({ nickname: null });
  render(<UserSignature user={user} onSaved={vi.fn()} />);
  fireEvent.click(screen.getByRole('button', { name: 'Очистить' }));
  await waitFor(() => expect(screen.getByLabelText('Подпись в MAX')).toHaveValue(''));
  expect(mock).toHaveBeenCalledWith('/api/telegram/users/42/nickname', { method: 'PATCH', body: JSON.stringify({ nickname: null }) });
  expect(screen.getByText('Предпросмотр: [Ромка 💀 Шалапай 😈]: Текст сообщения')).toBeInTheDocument();
  expect(screen.getByText(/Пустая подпись возвращает обычное имя/)).toBeInTheDocument();
});

it('rejects control characters without submitting and reports API errors', async () => {
  render(<UserSignature user={user} onSaved={vi.fn()} />);
  fireEvent.change(screen.getByLabelText('Подпись в MAX'), { target: { value: 'bad\x00name' } });
  fireEvent.click(screen.getByRole('button', { name: 'Сохранить подпись' }));
  expect(screen.getByRole('alert')).toHaveTextContent('До 64 символов');
  expect(mock).not.toHaveBeenCalled();
  mock.mockRejectedValue(new Error('Не удалось сохранить'));
  fireEvent.change(screen.getByLabelText('Подпись в MAX'), { target: { value: 'Анна' } });
  fireEvent.click(screen.getByRole('button', { name: 'Сохранить подпись' }));
  await screen.findByText('Не удалось сохранить');
});

it('renders alongside ACL controls and keeps Telegram admin distinct', async () => {
  const { MemoryRouter } = await import('react-router-dom');
  const { UsersPage } = await import('./pages');
  mock.mockImplementation(async (url: string, options?: RequestInit) => {
    if (url === '/api/telegram/users') return { users: [{ ...user, role: 'read_only', approved: false, blocked: false, membership: 'administrator', is_admin: true }] } as any;
    if (url === '/api/telegram/member-sync') return { configured: false, running: false, last_sync_at: 0, status: 'not_configured' } as any;
    return {} as any;
  });
  render(<MemoryRouter><UsersPage /></MemoryRouter>);
  await screen.findByLabelText('Подпись в MAX');
  expect(screen.getByText('Telegram: administrator · администратор группы')).toBeInTheDocument();
  expect(screen.getByLabelText('Роль')).toHaveValue('read_only');
  fireEvent.click(screen.getByLabelText('Одобрить'));
  await waitFor(() => expect(mock).toHaveBeenCalledWith('/api/telegram/users/42/approval', {
    method: 'PATCH', body: JSON.stringify({ approved: true, role: 'operator' }),
  }));
});
