import { beforeAll, vi } from 'vitest';

beforeAll(() => {
  vi.stubGlobal('fetch', async () => ({ ok: true }));
});
