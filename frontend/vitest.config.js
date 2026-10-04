import { defineConfig } from 'vitest/config';

// jsdom, not a browser: every test here drives a hook's timers and socket
// lifecycle, which needs a DOM and a controllable clock, not a renderer.
export default defineConfig({
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['src/**/*.test.{js,jsx}'],
    // A frontend test that reaches the network is a flake waiting for a
    // slow day; there is no server in CI to reach.
    restoreMocks: true,
  },
});
