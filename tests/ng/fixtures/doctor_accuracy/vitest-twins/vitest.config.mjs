import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    include: ['other/**/*.test.mjs'],
  },
});
