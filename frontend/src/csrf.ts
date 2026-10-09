// CSRF token — single in-memory holder. The token is fetched
// from the backend after a successful login and attached by the
// api wrapper to every mutating request.

let _csrf: string | null = null;

export function setCsrfToken(token: string | null): void {
  _csrf = token && token.length ? token : null;
}

export function clearCsrfToken(): void {
  _csrf = null;
}

export function getCsrfToken(): string | null {
  return _csrf;
}
