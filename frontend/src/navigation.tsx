import { createContext, useCallback, useContext, useEffect, useId, useState, type ReactNode } from 'react';
import { useBlocker, useLocation, useBeforeUnload } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

const NavigationSafety = createContext({ setDirty: (_id: string, _dirty: boolean) => {} });
export function useNavigationSafety() {
  const { setDirty } = useContext(NavigationSafety);
  const id = useId();
  useEffect(() => () => setDirty(id, false), [id, setDirty]);
  return { markDirty: () => setDirty(id, true), markSaved: () => setDirty(id, false) };
}

/** One guard for links, programmatic navigation, browser Back and reload. */
export function NavigationSafetyProvider({ children }: { children: ReactNode }) {
  const [scopes, setScopes] = useState<Set<string>>(new Set());
  const dirty = scopes.size > 0;
  const setDirty = useCallback((id: string, value: boolean) => setScopes(current => {
    if (current.has(id) === value) return current;
    const next = new Set(current);
    if (value) next.add(id); else next.delete(id);
    return next;
  }), []);
  const location = useLocation();
  const { t } = useTranslation();
  const blocker = useBlocker(({ currentLocation, nextLocation }) =>
    dirty && currentLocation.pathname !== nextLocation.pathname && nextLocation.pathname !== '/login');
  useEffect(() => { setScopes(new Set()); }, [location.key]);
  useEffect(() => {
    if (blocker.state !== 'blocked') return;
    if (window.confirm(t('setupUx.discard'))) blocker.proceed();
    else blocker.reset();
  }, [blocker, t]);
  useBeforeUnload(event => { if (dirty) { event.preventDefault(); event.returnValue = ''; } });
  return <NavigationSafety.Provider value={{ setDirty }}>
    {children}
  </NavigationSafety.Provider>;
}
