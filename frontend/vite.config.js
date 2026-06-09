import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// SECURITY NOTE (VUL-014):
// The dev server proxies /ws to ws://localhost:8000 (plain WebSocket).
// The API key transmitted in the upgrade query param is sent in cleartext.
// NEVER expose the Vite dev server on a non-loopback interface.
// For any networked dev environment, enable HTTPS:
//   server: { https: { key: './certs/key.pem', cert: './certs/cert.pem' } }
// and change proxy targets to wss:// and https://.

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    host: "127.0.0.1",   // loopback only — never expose dev server externally
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
        rewrite: path => path.replace(/^\/api/, ""),
      },
      "/ws": {
        target: "ws://localhost:8000",
        ws: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
