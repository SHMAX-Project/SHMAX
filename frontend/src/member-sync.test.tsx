import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import '@testing-library/jest-dom/vitest';
import { MemberSync } from './member-sync';
import { api } from './api';
vi.mock('./api', () => ({ api: vi.fn() }));
const mock = vi.mocked(api);
const state = { configured: true, running: false, api_id: 123, api_hash_saved: true, status: 'ok', last_sync_at: 1, found: 53, new: 4, updated: 8 };
beforeEach(() => { cleanup(); mock.mockReset(); });
it('shows calm missing configuration and settings navigation', async () => {
  mock.mockResolvedValue({ ...state, configured: false, last_sync_at: 0 });
  render(<MemoryRouter><MemberSync /></MemoryRouter>);
  expect(await screen.findByText(/Полная синхронизация участников не настроена/)).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Настройки Telegram' })).toHaveAttribute('href', '/telegram');
  expect(screen.queryByRole('button', { name: 'Синхронизировать с Telegram' })).not.toBeInTheDocument();
});
it('runs manual sync, displays statistics and reloads users', async () => {
  mock.mockResolvedValue(state); const reload = vi.fn();
  render(<MemoryRouter><MemberSync onSync={reload} /></MemoryRouter>);
  fireEvent.click(await screen.findByRole('button', { name: 'Синхронизировать с Telegram' }));
  await waitFor(() => expect(reload).toHaveBeenCalledOnce());
  expect(mock).toHaveBeenCalledWith('/api/telegram/member-sync', { method: 'POST' });
  expect(screen.getByText(/Найдено: 53 · Новых: 4 · Обновлено: 8/)).toBeInTheDocument();
});
it('keeps saved hash hidden and clears submitted hash after save', async () => {
  mock.mockResolvedValue(state);
  render(<MemoryRouter><MemberSync settings /></MemoryRouter>);
  await screen.findByRole('button', { name: 'Синхронизировать с Telegram' });
  const hash = screen.getByLabelText('Telegram API Hash');
  expect(hash).toHaveValue(''); expect(hash).toHaveAttribute('type', 'password');
  fireEvent.change(hash, { target: { value: 'a'.repeat(32) } });
  fireEvent.click(screen.getByRole('button', { name: 'Сохранить настройки синхронизации' }));
  await waitFor(() => expect(hash).toHaveValue(''));
  expect(screen.getByText(/Обнаружение не даёт доступ/)).toBeInTheDocument();
});
it('disables simultaneous sync while a run is active', async () => {
  mock.mockResolvedValue({ ...state, running: true });
  render(<MemoryRouter><MemberSync /></MemoryRouter>);
  expect(await screen.findByRole('button', { name: 'Синхронизировать с Telegram' })).toBeDisabled();
});
