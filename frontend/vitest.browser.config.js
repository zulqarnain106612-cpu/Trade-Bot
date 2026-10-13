import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import { playwright } from '@vitest/browser-playwright';

export default defineConfig({
  plugins: [react()],
  test: {
    include: ['browser-e2e/**/*.e2e.jsx'],
    restoreMocks: true,
    clearMocks: true,
    browser: {
      enabled: true,
      provider: playwright(),
      instances: [{ browser: 'chromium' }],
      headless: true,
      viewport: { width: 1440, height: 1000 },
    },
  },
});
