import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    include: ['src/**/*.test.ts'],
    exclude: [...configDefaults.exclude, 'e2e/**'],
    setupFiles: ['./src/setup.ts'],
  },
});
