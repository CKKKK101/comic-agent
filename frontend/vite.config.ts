import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// dev 时后端跑在 18766;build 产物由 FastAPI 以同源 / 托管
const BACKEND = "http://127.0.0.1:18766";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": BACKEND,
      "/static": BACKEND,
    },
  },
  build: { outDir: "dist" },
});
