/**
 * Dependency injection boundary for repositories.
 *
 * Screens consume repositories ONLY through this context, so the concrete
 * implementation (mock today, API client later) can be swapped at the app root
 * without touching any screen. This is the "UI doesn't know where data lives"
 * boundary from CLAUDE.md § 将来 API / sync を入れても壊れにくい境界.
 */
import React, { createContext, useContext, useMemo } from 'react';
import { PlayerRepository } from '../repositories/PlayerRepository';
import { MockPlayerRepository } from '../repositories/MockPlayerRepository';

interface Repositories {
  players: PlayerRepository;
}

const RepositoryContext = createContext<Repositories | null>(null);

interface RepositoryProviderProps {
  children: React.ReactNode;
  /** Override for tests / future API wiring. Defaults to the in-memory mock. */
  repositories?: Repositories;
}

export function RepositoryProvider({
  children,
  repositories,
}: RepositoryProviderProps) {
  const value = useMemo<Repositories>(
    () => repositories ?? { players: new MockPlayerRepository() },
    [repositories],
  );
  return (
    <RepositoryContext.Provider value={value}>
      {children}
    </RepositoryContext.Provider>
  );
}

export function usePlayerRepository(): PlayerRepository {
  const ctx = useContext(RepositoryContext);
  if (!ctx) {
    throw new Error('usePlayerRepository must be used within a RepositoryProvider');
  }
  return ctx.players;
}
