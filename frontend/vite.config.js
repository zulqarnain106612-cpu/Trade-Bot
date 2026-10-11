import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// SECURITY NOTE (VUL-014):
// The dev server proxies /ws to the API over plain WebSocket.
// The API key transmitted in the upgrade query param is sent in cleartext.
// NEVER expose the Vite dev server on a non-loopback interface.
// For any networked dev environment, enable HTTPS:
//   server: { https: { key: './certs/key.pem', cert: './certs/cert.pem' } }
// and change proxy targets to wss:// and https://.

export default defineConfig(({ mode }) => {
  // The proxy targets the same API origin the app itself calls
  // (VITE_API_URL, see src/hooks/useApi.js) -- a second, hard-coded port
  // here could silently disagree with it.
  const api = loadEnv(mode, process.cwd(), "VITE_").VITE_API_URL || "http://localhost:8000";
  return {
    plugins: [react()],
    server: {
      open: false,
      port: 5173,
      host: "127.0.0.1",   // loopback only — never expose dev server externally
      proxy: {
        "/api": {
          target: api,
          changeOrigin: true,
          rewrite: path => path.replace(/^\/api/, ""),
        },
        "/ws": {
          target: api.replace(/^http/, "ws"),
          ws: true,
        },
      },
    },
    build: {
      outDir: "dist",
      sourcemap: false,
    },
  };
});
