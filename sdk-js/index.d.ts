export type Level = 'debug' | 'info' | 'warning' | 'error' | 'fatal';

export interface InitOptions {
  /** https://KEY@monitor.example.com/PROJECT_ID */
  dsn: string;
  environment?: string;
  release?: string;
  tags?: Record<string, string>;
  /** Edit an event before it is sent; return null to drop it. */
  beforeSend?: (event: Record<string, unknown>) => Record<string, unknown> | null;
}

export interface CaptureOptions {
  level?: Level;
  tags?: Record<string, string>;
  extra?: Record<string, unknown>;
  request?: { url?: string; method?: string };
}

export function init(options: InitOptions): void;
export function captureException(error: unknown, options?: CaptureOptions): string | undefined;
export function captureMessage(message: string, level?: Level, options?: CaptureOptions): string | undefined;
export function setUser(user: { id?: string; email?: string; username?: string } | null): void;
export function setTag(key: string, value: string): void;
export function addBreadcrumb(crumb: { category?: string; message?: string; level?: Level; data?: unknown }): void;
export function flush(timeoutMs?: number): Promise<void>;
export function onRequestError(
  error: unknown,
  request: { path: string; method: string; headers: Record<string, string | string[] | undefined> },
  context: { routerKind: string; routePath: string; routeType: string },
): Promise<void>;
